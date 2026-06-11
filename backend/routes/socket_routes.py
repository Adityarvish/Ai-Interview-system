import base64
import logging
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import gevent

from flask_socketio import emit, join_room
from flask import request

from config.settings import Config
from services.mongo_service import MongoService
from services.interview_engine import InterviewEngine
from services.evaluator import EvaluatorService
from services.speech_to_text import stt_service
from services.text_to_speech import tts_service

logger = logging.getLogger(__name__)

ENGINES:              dict[str, InterviewEngine] = {}
AUDIO_BUFFERS:        dict[str, list[bytes]]     = {}
CANCELLED_INTERVIEWS: set[str]                   = set()
SID_TO_INTERVIEW:     dict[str, str]             = {}

_PROCESSING_INTERVIEWS: set[str] = set()

_EVALUATING_INTERVIEWS: set[str] = set()


def _safe_emit(socketio, sid, event: str, payload: dict):
    """
    Emit an event to the correct target.

    FIX v15: Always emit to the interview room when interview_id is present.
    The room contains the client's sid (added by join_room() in on_join_interview),
    so the client receives the event exactly once via room membership.

    Previously this function emitted to the room, and _emit_to_room() then called
    _safe_emit(sid) a second time — which emitted to the ROOM AGAIN (since
    interview_id was in the payload). This caused evaluation_ready, status, and
    other events to arrive 2-3× per emission.

    If no interview_id is present, fall back to direct sid targeting.
    If neither is available, broadcast (logged as a warning).
    """
    interview_id = payload.get('interview_id')
    if interview_id:
        socketio.emit(event, payload, to=interview_id, namespace='/')
    elif sid:
        socketio.emit(event, payload, to=sid, namespace='/')
    else:
        logger.warning(f"[EMIT] No sid or interview_id — broadcasting {event}")
        socketio.emit(event, payload, namespace='/')


def _is_cancelled(interview_id: str) -> bool:
    return interview_id in CANCELLED_INTERVIEWS


def _cleanup_session(interview_id: str):
    """FIX BUG #6: Atomically clear all session state for an interview."""
    ENGINES.pop(interview_id, None)
    AUDIO_BUFFERS.pop(interview_id, None)
    CANCELLED_INTERVIEWS.discard(interview_id)
    _PROCESSING_INTERVIEWS.discard(interview_id)
    _EVALUATING_INTERVIEWS.discard(interview_id)


def _delete_file_safe(path: str):
    """Delete a file without raising — used for temp audio cleanup."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception as e:
        logger.debug(f"[CLEANUP] Could not delete {path}: {e}")


def _convert_to_wav_blocking(input_path: str, output_path: str, timeout: int = 30) -> bool:
    """
    Run ffmpeg synchronously. This function MUST be called via the gevent
    threadpool (gevent.get_hub().threadpool.apply()) — never directly from a
    greenlet, because subprocess.run() with pipe I/O is a blocking OS call that
    bypasses gevent's hub and freezes ALL greenlets (including ping/pong).
    """
    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-ar', '16000',
        '-ac', '1',
        '-f', 'wav',
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.error(
                f"[FFMPEG] Conversion failed (rc={result.returncode})\n"
                f"STDERR: {result.stderr[-500:]}"
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"[FFMPEG] Conversion timed out after {timeout}s")
        return False
    except FileNotFoundError:
        logger.error("[FFMPEG] ffmpeg not found — install: apt-get install ffmpeg")
        return False
    except Exception as e:
        logger.error(f"[FFMPEG] Unexpected error: {e}")
        return False


def _convert_to_wav(input_path: str, output_path: str, timeout: int = 30) -> bool:
    """
    ROOT CAUSE FIX (v12): Run ffmpeg in a real OS thread via the gevent
    threadpool. This suspends only the calling greenlet (cooperative yield),
    leaving the gevent hub free to service Engine.IO ping/pong, keepalive
    emits, and all other SocketIO events during the (potentially 30-180s)
    ffmpeg conversion of large audio files.

    Without this fix, subprocess.run() blocks the single OS thread that gevent
    uses, starving the event loop and causing the Engine.IO ping timeout to fire
    => client disconnect => "Connection lost" during long answers.
    """
    try:
        from gevent import get_hub
        pool = get_hub().threadpool
        return pool.apply(_convert_to_wav_blocking, (input_path, output_path, timeout))
    except Exception as e:
        logger.error(f"[FFMPEG] threadpool.apply failed: {e} — falling back to blocking call")
        return _convert_to_wav_blocking(input_path, output_path, timeout)


def _validate_audio_bytes(data: bytes, min_size: int = 500) -> tuple[bool, str]:
    if not data:
        return False, "No audio data received."
    if len(data) < min_size:
        return False, f"Audio too short ({len(data)} bytes). Please record a longer answer."
    return True, ""


def _tts_b64(text: str, interview_id: str) -> tuple[str | None, str | None]:
    """
    Generate TTS via Groq Orpheus and return (base64_audio, format).
    Never raises — returns (None, None) on failure.

    Logs [TTS_PROVIDER], [TTS_FILE] with MD5 hash, and [TTS_COMPLETED] on every call.
    """
    import hashlib
    from services.text_to_speech import GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT

    t0       = time.perf_counter()
    out_name = f"ai_{interview_id}_{uuid.uuid4().hex}.wav"

    logger.info(
        "[TTS_PROVIDER] [%s] provider=GroqOrpheus  model=%s  voice=%s  fmt=%s  chars=%d",
        interview_id, GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT, len(text)
    )

    try:
        path = tts_service.generate_speech(text, out_name)

        if path is None:
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.error(
                "[TTS_FAILED] [%s] after %dms — generate_speech() returned None. "
                "Check GROQ_API_KEY in backend/.env and that groq>=0.9.0 is installed.",
                interview_id, elapsed
            )
            return None, None

        suffix = Path(path).suffix.lstrip('.')
        with open(path, 'rb') as f:
            audio_bytes = f.read()

        audio_hash = hashlib.md5(audio_bytes).hexdigest()
        timestamp  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        b64        = base64.b64encode(audio_bytes).decode('ascii')
        size_bytes = len(audio_bytes)
        elapsed    = int((time.perf_counter() - t0) * 1000)

        logger.info(
            "[TTS_FILE] [%s]  [TTS_PROVIDER=GroqOrpheus]  [TTS_MODEL=%s]  "
            "[TTS_VOICE=%s]  [AUDIO_FILE=%s]  [AUDIO_SIZE=%d]  "
            "[AUDIO_TIMESTAMP=%s]  [AUDIO_HASH=%s]",
            interview_id, GROQ_TTS_MODEL, GROQ_TTS_VOICE,
            out_name, size_bytes, timestamp, audio_hash
        )
        logger.info("Generated audio hash: %s", audio_hash)
        logger.info(
            "[TTS_COMPLETED] [%s] provider=GroqOrpheus  elapsed_ms=%d  "
            "size_kb=%d  fmt=%s  voice=%s  hash=%s",
            interview_id, elapsed, size_bytes // 1024, suffix, GROQ_TTS_VOICE, audio_hash
        )
        _delete_file_safe(path)
        return b64, suffix
    except Exception as e:
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "[TTS_FAILED] [%s] after %dms — %s\n"
            "             Frontend will fall back to browser speechSynthesis.\n"
            "             Run: curl http://localhost:5000/api/tts/health",
            interview_id, elapsed, e
        )
        return None, None


def register_socket_events(socketio):


    @socketio.on('connect')
    def on_connect():
        sid       = getattr(request, 'sid', 'unknown')
        transport = request.environ.get('HTTP_UPGRADE', 'polling')
        logger.info(f"[WS] Connected  sid={sid}  transport={transport}")

    @socketio.on('disconnect')
    def on_disconnect():
        sid = getattr(request, 'sid', 'unknown')
        logger.info(f"[WS] Disconnected  sid={sid}")
        SID_TO_INTERVIEW.pop(sid, None)


    @socketio.on('join_interview')
    def on_join_interview(data):
        interview_id = (data.get('interview_id') or '').strip()
        sid          = getattr(request, 'sid', None)
        if not interview_id or not sid:
            return
        join_room(interview_id)
        SID_TO_INTERVIEW[sid] = interview_id
        logger.info(f"[WS] sid={sid} joined room interview_id={interview_id}")
        emit('joined_interview', {'interview_id': interview_id, 'sid': sid})


    @socketio.on('audio_chunk')
    def on_audio_chunk(data):
        interview_id = (data.get('interview_id') or '').strip()
        chunk_b64    = data.get('data', '')
        if not interview_id or not chunk_b64:
            return
        try:
            chunk_bytes = base64.b64decode(chunk_b64)
        except Exception:
            return
        AUDIO_BUFFERS.setdefault(interview_id, []).append(chunk_bytes)

    @socketio.on('audio_end')
    def on_audio_end(data):
        interview_id = (data.get('interview_id') or '').strip()
        if not interview_id:
            emit('error', {'interview_id': '', 'message': 'interview_id required'})
            return
        chunks = AUDIO_BUFFERS.pop(interview_id, [])
        if not chunks:
            emit('error', {'interview_id': interview_id,
                           'message': 'No audio received. Please use a supported browser.'})
            return
        audio_bytes = b''.join(chunks)
        sid = getattr(request, 'sid', None)
        _handle_audio_bytes(
            socketio, sid, interview_id, audio_bytes, extension='webm',
            elapsed=int(data.get('elapsed_time') or 0)
        )


    @socketio.on('audio_upload')
    def on_audio_upload(data):
        interview_id = (data.get('interview_id') or '').strip()
        audio_b64    = data.get('audio_data', '')
        extension    = (data.get('extension') or 'webm').strip().lower()
        elapsed      = int(data.get('elapsed_time') or 0)

        if not interview_id:
            emit('error', {'interview_id': '', 'message': 'interview_id required'})
            return

        engine = ENGINES.get(interview_id)
        if not engine:
            emit('error', {'interview_id': interview_id,
                           'message': 'Interview session not found. Please refresh and try again.'})
            return

        if not audio_b64:
            emit('error', {'interview_id': interview_id, 'message': 'No audio data received.'})
            return

        if interview_id in _PROCESSING_INTERVIEWS:
            logger.warning(
                f"[UPLOAD] [{interview_id}] Already processing — dropping duplicate upload. "
                f"(double-click or reconnect race)"
            )
            emit('status', {'interview_id': interview_id,
                            'message': 'Still processing your previous answer…'})
            return

        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as e:
            logger.warning(f"[UPLOAD] [{interview_id}] Base64 decode failed: {e}")
            emit('error', {'interview_id': interview_id,
                           'message': 'Audio upload was corrupted. Please try again.'})
            return

        sid = getattr(request, 'sid', None)
        _handle_audio_bytes(socketio, sid, interview_id, audio_bytes, extension, elapsed)


    def _handle_audio_bytes(socketio, sid, interview_id, audio_bytes, extension, elapsed):
        valid, validation_msg = _validate_audio_bytes(audio_bytes)
        if not valid:
            _safe_emit(socketio, sid, 'error',
                       {'interview_id': interview_id, 'message': validation_msg})
            return

        if extension not in ('webm', 'ogg', 'mp4', 'wav'):
            extension = 'webm'

        raw_path = Config.AUDIO_FOLDER / f"{interview_id}_{uuid.uuid4().hex}.{extension}"
        raw_path.write_bytes(audio_bytes)
        logger.info(f"[AUDIO] [{interview_id}] Wrote {len(audio_bytes)} bytes → {raw_path.name}")

        engine = ENGINES.get(interview_id)

        def _process():
            t_bg = time.perf_counter()

            _PROCESSING_INTERVIEWS.add(interview_id)

            _keepalive_running = [True]
            _current_status    = ['Transcribing your answer…']
            _keepalive_greenlet = [None]

            wav_path_str = [None]

            try:
                if _is_cancelled(interview_id):
                    logger.info(f"[PROCESS] [{interview_id}] Cancelled before STT")
                    return

                def _keepalive_loop():
                    while _keepalive_running[0]:
                        gevent.sleep(5)
                        if _keepalive_running[0] and not _is_cancelled(interview_id):
                            socketio.emit('status',
                                          {'interview_id': interview_id,
                                           'message': _current_status[0]},
                                          to=interview_id, namespace='/')
                            socketio.emit('heartbeat',
                                          {'interview_id': interview_id,
                                           'ts': int(time.time() * 1000)},
                                          to=interview_id, namespace='/')

                _keepalive_greenlet[0] = gevent.spawn(_keepalive_loop)

                _current_status[0] = 'Transcribing your answer…'
                _safe_emit(socketio, sid, 'status',
                           {'interview_id': interview_id,
                            'message': 'Transcribing your answer…'})

                wav_path = Config.AUDIO_FOLDER / f"{interview_id}_{uuid.uuid4().hex}.wav"
                wav_path_str[0] = str(wav_path)
                t_conv = time.perf_counter()
                converted = _convert_to_wav(str(raw_path), str(wav_path))
                conv_ms = int((time.perf_counter() - t_conv) * 1000)

                if converted:
                    transcribe_path = str(wav_path)
                    logger.info(f"[FFMPEG] [{interview_id}] WAV conversion: {conv_ms}ms")
                else:
                    logger.warning(f"[FFMPEG] [{interview_id}] WAV conversion failed — "
                                   "using original")
                    transcribe_path = str(raw_path)
                    wav_path_str[0] = None

                if _is_cancelled(interview_id):
                    return

                t_stt = time.perf_counter()
                try:
                    transcript = stt_service.transcribe(transcribe_path)
                except Exception as e:
                    logger.exception(f"[STT] [{interview_id}] Transcription failed: {e}")
                    _safe_emit(socketio, sid, 'error', {
                        'interview_id': interview_id,
                        'message': (
                            'Unable to process audio. Please try recording again. '
                            'If this persists, check that your microphone is working correctly.'
                        ),
                    })
                    return

                stt_ms = int((time.perf_counter() - t_stt) * 1000)
                logger.info(f"[STT] [{interview_id}] {stt_ms}ms → '{transcript[:80]}'")

                _delete_file_safe(str(raw_path))
                if wav_path_str[0]:
                    _delete_file_safe(wav_path_str[0])

                if not transcript or len(transcript.strip()) < 2:
                    _safe_emit(socketio, sid, 'error', {
                        'interview_id': interview_id,
                        'message': 'Could not detect speech in your recording. '
                                   'Please speak clearly and try again.',
                    })
                    return

                _safe_emit(socketio, sid, 'transcript',
                           {'interview_id': interview_id, 'transcript': transcript})

                if _is_cancelled(interview_id):
                    return

                current_q = (engine.state.questions_asked[-1]
                             if engine.state.questions_asked else '')
                engine.process_answer(transcript)

                def _save_interaction():
                    try:
                        _mongo.save_interview_interaction(
                            interview_id=interview_id,
                            question=current_q,
                            answer=transcript,
                            transcript=transcript,
                            audio_path='[deleted]',
                            scores={},
                        )
                    except Exception as e:
                        logger.error(f"[MONGO] save_interaction failed: {e}")
                gevent.spawn(_save_interaction)

                if _is_cancelled(interview_id):
                    return

                count    = len(engine.state.questions_asked)
                is_final = not engine.should_continue_interview(elapsed, count)

                _current_status[0] = 'Generating next question…'
                _safe_emit(socketio, sid, 'status',
                           {'interview_id': interview_id,
                            'message': 'Generating next question…'})

                t_llm = time.perf_counter()
                if is_final:
                    ai_text = engine.generate_closing_statement()
                else:
                    ai_text = engine.generate_follow_up_question(transcript, count + 1)
                llm_ms = int((time.perf_counter() - t_llm) * 1000)
                logger.info(f"[LLM] [{interview_id}] Q{count+1} in {llm_ms}ms: '{ai_text[:80]}'")

                _keepalive_running[0] = False
                if _keepalive_greenlet[0]:
                    _keepalive_greenlet[0].kill(block=False)
                    _keepalive_greenlet[0] = None

                if _is_cancelled(interview_id):
                    return

                inline_audio_b64  = None
                inline_audio_fmt  = 'wav'
                inline_tts_ms     = None

                if is_final and not _is_cancelled(interview_id):
                    _safe_emit(socketio, sid, 'status',
                               {'interview_id': interview_id,
                                'message': 'Generating closing audio…'})
                    t_inline_tts = time.perf_counter()
                    inline_audio_b64, inline_audio_fmt = _tts_b64(ai_text, interview_id)
                    inline_tts_ms = int((time.perf_counter() - t_inline_tts) * 1000)
                    inline_audio_fmt = inline_audio_fmt or 'wav'
                    logger.info(
                        f"[TTS] [{interview_id}] Closing statement TTS (inline): "
                        f"{inline_tts_ms}ms audio={'YES' if inline_audio_b64 else 'NONE'}"
                    )

                _safe_emit(socketio, sid, 'next_question', {
                    'interview_id':    interview_id,
                    'question':        ai_text,
                    'audio':           inline_audio_b64,
                    'audio_format':    inline_audio_fmt,
                    'is_final':        is_final,
                    'question_count':  count,
                    'interview_stage': engine.state.stage,
                    'timing': {
                        'stt_ms':   stt_ms,
                        'llm_ms':   llm_ms,
                        'tts_ms':   inline_tts_ms,
                        'total_ms': int((time.perf_counter() - t_bg) * 1000),
                    },
                })

                def _deliver_audio():
                    if is_final and inline_audio_b64 is not None:
                        logger.debug(
                            f"[TTS] [{interview_id}] Closing audio already sent inline — "
                            f"skipping backup _deliver_audio greenlet"
                        )
                        return
                    if _is_cancelled(interview_id):
                        return
                    t_tts = time.perf_counter()
                    _safe_emit(socketio, sid, 'status',
                               {'interview_id': interview_id,
                                'message': 'Generating audio…'})
                    audio_b64, audio_fmt = _tts_b64(ai_text, interview_id)
                    tts_ms = int((time.perf_counter() - t_tts) * 1000)
                    if not _is_cancelled(interview_id):
                        _safe_emit(socketio, sid, 'next_question_audio', {
                            'interview_id': interview_id,
                            'audio':        audio_b64,
                            'audio_format': audio_fmt or 'wav',
                            'is_final':     is_final,
                        })
                    logger.info(f"[TTS] [{interview_id}] backup greenlet {tts_ms}ms "
                                f"audio={'YES' if audio_b64 else 'NONE'}")

                gevent.spawn(_deliver_audio)

            except Exception as e:
                logger.exception(f"[PROCESS] [{interview_id}] Unexpected error: {e}")
                _safe_emit(socketio, sid, 'error', {
                    'interview_id': interview_id,
                    'message': 'An unexpected error occurred. Please try again.',
                })
            finally:
                _keepalive_running[0] = False
                if _keepalive_greenlet[0]:
                    try:
                        _keepalive_greenlet[0].kill(block=False)
                    except Exception:
                        pass

                _PROCESSING_INTERVIEWS.discard(interview_id)

                _delete_file_safe(str(raw_path))
                if wav_path_str[0]:
                    _delete_file_safe(wav_path_str[0])

                total_ms = int((time.perf_counter() - t_bg) * 1000)
                logger.info(f"[PROCESS] [{interview_id}] Greenlet done in {total_ms}ms")

        socketio.start_background_task(_process)


    @socketio.on('end_interview')
    def on_end_interview(data):
        interview_id = (data.get('interview_id') or '').strip()
        if not interview_id:
            emit('error', {'interview_id': '', 'message': 'interview_id required'})
            return
        sid = getattr(request, 'sid', None)

        CANCELLED_INTERVIEWS.add(interview_id)
        AUDIO_BUFFERS.pop(interview_id, None)
        logger.info(f"[END] [{interview_id}] Cancellation flag set")

        def _finalize():
            if interview_id in _EVALUATING_INTERVIEWS:
                logger.warning(f"[END] [{interview_id}] Already evaluating — dropping duplicate")
                return
            _EVALUATING_INTERVIEWS.add(interview_id)
            try:
                def _emit_to_room(event, payload):
                    socketio.emit(event, payload, to=interview_id, namespace='/')

                _emit_to_room('status', {
                    'interview_id': interview_id,
                    'message': 'Generating final evaluation…',
                })

                _wait_start = time.perf_counter()
                while interview_id in _PROCESSING_INTERVIEWS:
                    gevent.sleep(0.2)
                    if time.perf_counter() - _wait_start > 30:
                        logger.warning(f"[END] [{interview_id}] Timed out waiting "
                                       "for _process() to finish — proceeding anyway")
                        break

                engine       = ENGINES.get(interview_id)
                interactions = _mongo.get_all_interactions(interview_id)
                logger.info(f"[END] [{interview_id}] {len(interactions)} interactions")

                if engine:
                    name = engine.candidate_info.get('name', 'Candidate')
                    jd   = engine.candidate_info.get('job_description', '')
                else:
                    session = _mongo.get_interview_session(interview_id)
                    if not session:
                        _emit_to_room('error', {
                            'interview_id': interview_id,
                            'message': 'Interview session not found.',
                        })
                        return
                    cand = _mongo.get_candidate_by_id(session.get('candidate_id'))
                    name = (cand or {}).get('name', 'Candidate')
                    jd   = (cand or {}).get('job_description', '')

                t_eval     = time.perf_counter()
                evaluator  = EvaluatorService()
                evaluation = evaluator.generate_final_evaluation(interactions, name, jd)
                evaluation['interview_status'] = 'completed'
                saved_doc  = _mongo.save_final_evaluation(interview_id, evaluation)
                eval_ms    = int((time.perf_counter() - t_eval) * 1000)

                logger.info(f"[END] [{interview_id}] eval in {eval_ms}ms "
                            f"overall={saved_doc.get('overall_score')} "
                            f"rec={saved_doc.get('recommendation')}")

                _emit_to_room('evaluation_ready', {
                    'interview_id': interview_id,
                    'evaluation':   saved_doc,
                })
            except Exception as e:
                logger.exception(f"[END] [{interview_id}] Failed: {e}")
                socketio.emit('error', {
                    'interview_id': interview_id,
                    'message': 'Failed to generate evaluation. Please try again.',
                }, to=interview_id, namespace='/')
                if sid:
                    _safe_emit(socketio, sid, 'error', {
                        'interview_id': interview_id,
                        'message': 'Failed to generate evaluation. Please try again.',
                    })
            finally:
                _cleanup_session(interview_id)

        socketio.start_background_task(_finalize)


    @socketio.on('cancel_interview')
    def on_cancel_interview(data):
        interview_id = (data.get('interview_id') or '').strip()
        if not interview_id:
            emit('error', {'interview_id': '', 'message': 'interview_id required'})
            return
        sid = getattr(request, 'sid', None)

        CANCELLED_INTERVIEWS.add(interview_id)
        AUDIO_BUFFERS.pop(interview_id, None)
        logger.info(f"[CANCEL] [{interview_id}] Cancellation flag set")

        def _cancel():
            if interview_id in _EVALUATING_INTERVIEWS:
                logger.warning(f"[CANCEL] [{interview_id}] Already evaluating — dropping duplicate")
                return
            _EVALUATING_INTERVIEWS.add(interview_id)

            def _emit_to_room(event, payload):
                socketio.emit(event, payload, to=interview_id, namespace='/')

            try:
                _wait_start = time.perf_counter()
                while interview_id in _PROCESSING_INTERVIEWS:
                    gevent.sleep(0.2)
                    if time.perf_counter() - _wait_start > 30:
                        logger.warning(f"[CANCEL] [{interview_id}] Timed out waiting "
                                       "for _process() to finish — proceeding anyway")
                        break

                _mongo.mark_interview_cancelled(interview_id)
                engine       = ENGINES.get(interview_id)
                interactions = _mongo.get_all_interactions(interview_id)

                if engine:
                    name = engine.candidate_info.get('name', 'Candidate')
                    jd   = engine.candidate_info.get('job_description', '')
                else:
                    session = _mongo.get_interview_session(interview_id)
                    cand    = _mongo.get_candidate_by_id(
                        (session or {}).get('candidate_id')) if session else None
                    name    = (cand or {}).get('name', 'Candidate')
                    jd      = (cand or {}).get('job_description', '')

                evaluator  = EvaluatorService()
                evaluation = evaluator.generate_final_evaluation(interactions, name, jd)
                evaluation['interview_status'] = 'cancelled'
                saved_doc  = _mongo.save_final_evaluation(interview_id, evaluation)

                logger.info(f"[CANCEL] [{interview_id}] Done — interactions={len(interactions)}")
                _emit_to_room('evaluation_ready', {
                    'interview_id': interview_id,
                    'evaluation':   saved_doc,
                })
            except Exception as e:
                logger.exception(f"[CANCEL] [{interview_id}] Failed: {e}")
                _emit_to_room('error', {
                    'interview_id': interview_id,
                    'message': 'Failed to generate evaluation.',
                })
            finally:
                _cleanup_session(interview_id)

        socketio.start_background_task(_cancel)
