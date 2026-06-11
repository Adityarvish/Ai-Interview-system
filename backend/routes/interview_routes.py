from flask import request, jsonify
import logging
import time
import uuid
import base64
from pathlib import Path

import gevent

from config.settings import Config
from services.mongo_service import MongoService
from services.interview_engine import InterviewEngine
from services.evaluator import EvaluatorService
from services.text_to_speech import tts_service
from routes.socket_routes import ENGINES

logger = logging.getLogger(__name__)
ALLOWED_RESUME_EXT = {'pdf', 'txt'}


def _delete_file_safe(path: str) -> None:
    """Delete a file without raising — used for temp audio cleanup."""
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _ext_ok(filename, allowed):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def _safe_filename(name: str) -> str:
    return name.replace('/', '_').replace('\\', '_').replace('..', '_')


def _tts_b64(text: str, interview_id: str):
    """
    Generate TTS via Groq Orpheus and return (base64_audio, format).
    Never raises — returns (None, None) on failure.

    Logs [TTS_PROVIDER], [TTS_FILE] with MD5 hash, and [TTS_COMPLETED] on every call.
    If the voice sounds unchanged, compare the [AUDIO_HASH] values across requests.
    """
    import hashlib
    from services.text_to_speech import GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT

    t0 = time.perf_counter()
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
            "             Frontend will fall back to browser speechSynthesis.",
            interview_id, elapsed, e
        )
        return None, None


def init_routes(app, socketio=None):
    mongo = MongoService()

    @app.route('/api/health', methods=['GET'])
    def health():
        return jsonify({'success': True, 'status': 'healthy',
                        'service': 'AI Interview System v8.2'})

    @app.route('/api/start-interview', methods=['POST'])
    def start_interview():
        t_req = time.perf_counter()
        try:
            logger.info("[START] /api/start-interview received")

            name        = (request.form.get('candidate_name') or '').strip()
            jd          = (request.form.get('job_description') or '').strip()
            resume_file = request.files.get('resume')

            if not name or not jd or not resume_file:
                return jsonify({'success': False,
                                'error': 'candidate_name, job_description, and resume are required'}), 400
            if not _ext_ok(resume_file.filename, ALLOWED_RESUME_EXT):
                return jsonify({'success': False,
                                'error': 'Resume must be PDF or TXT'}), 400

            fname       = _safe_filename(resume_file.filename)
            resume_path = Config.RESUME_FOLDER / f"{uuid.uuid4().hex}_{fname}"
            resume_file.save(resume_path)
            logger.info(f"[START] Resume saved: {resume_path.name} "
                        f"({resume_path.stat().st_size} bytes) "
                        f"in {int((time.perf_counter()-t_req)*1000)} ms")

            t_init       = time.perf_counter()
            interview_id = str(uuid.uuid4())
            engine       = InterviewEngine()
            r            = engine.initialize_interview(name, str(resume_path), jd)
            if not r['success']:
                logger.error(f"[START] Engine init failed: {r}")
                return jsonify(r), 500
            logger.info(f"[START] Engine init in "
                        f"{int((time.perf_counter()-t_init)*1000)} ms")

            ENGINES[interview_id] = engine

            t_mongo   = time.perf_counter()
            candidate = mongo.create_candidate(
                name=name, resume_path=str(resume_path), job_description=jd)
            mongo.create_interview_session(
                candidate_id=candidate['candidate_id'], interview_id=interview_id)
            logger.info(f"[START] MongoDB in {int((time.perf_counter()-t_mongo)*1000)} ms")

            t_q     = time.perf_counter()
            first_q = engine.generate_first_question()
            logger.info(f"[START] First question in {int((time.perf_counter()-t_q)*1000)} ms: "
                        f"'{first_q[:80]}'")

            total_ms = int((time.perf_counter() - t_req) * 1000)
            logger.info(f"[START] HTTP response returned in {total_ms} ms "
                        f"— interview_id={interview_id}")

            return jsonify({
                'success':        True,
                'interview_id':   interview_id,
                'candidate_id':   candidate['candidate_id'],
                'first_question': first_q,
                'timing': {'startup_ms': total_ms},
            })

        except Exception as e:
            logger.exception(f"start_interview failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/final-report/<interview_id>', methods=['GET'])
    def final_report(interview_id):
        try:
            doc = mongo.get_final_evaluation(interview_id)

            if not doc:
                logger.warning(f"[REPORT] No evaluation for {interview_id} — rebuilding")
                session = mongo.get_interview_session(interview_id)
                if session and session.get('status') in ('completed', 'in_progress'):
                    interactions = mongo.get_all_interactions(interview_id)
                    cand  = mongo.get_candidate_by_id(session.get('candidate_id'))
                    name  = (cand or {}).get('name', 'Candidate')
                    jd    = (cand or {}).get('job_description', '')
                    evaluator  = EvaluatorService()
                    evaluation = evaluator.generate_final_evaluation(interactions, name, jd)
                    evaluation['interview_status'] = 'completed'
                    doc = mongo.save_final_evaluation(interview_id, evaluation)
                    logger.info(f"[REPORT] Fail-safe evaluation generated for {interview_id}")
                else:
                    return jsonify({'success': False,
                                    'error': 'Evaluation not found'}), 404

            return jsonify({'success': True, 'evaluation': doc})
        except Exception as e:
            logger.exception(f"final_report failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/tts', methods=['POST'])
    def api_tts():
        """
        Generate Groq TTS audio on demand.
        Body: { "text": "...", "interview_id": "..." }
        Returns: { "audio": "<base64>", "audio_format": "wav" }
        Always uses Groq — never browser speech synthesis.
        """
        body         = request.get_json(force=True, silent=True) or {}
        text         = (body.get('text') or '').strip()
        interview_id = (body.get('interview_id') or 'ondemand')

        if not text:
            return jsonify({'success': False, 'error': 'text is required'}), 400

        audio_b64, audio_fmt = _tts_b64(text, interview_id)

        if not audio_b64:
            return jsonify({'success': False,
                            'error': 'TTS failed — check GROQ_API_KEY in backend/.env'}), 500

        return jsonify({
            'success':      True,
            'audio':        audio_b64,
            'audio_format': audio_fmt or 'wav',
        })


    @app.route('/debug-tts')
    def debug_tts():
        """
        Diagnostic route. Generates fresh audio every request.
        Returns filename, file_size, MD5 hash, provider, voice, model.

        Usage:
          curl http://localhost:5000/debug-tts
          curl "http://localhost:5000/debug-tts?text=Custom+test+sentence"
        """
        import hashlib, uuid as _uuid, time as _time
        from services.text_to_speech import (
            tts_service as _tts_svc,
            GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT,
            get_tts_debug_report,
        )

        text      = request.args.get('text', 'This is a test sentence.')
        out_name  = f"debug_{_uuid.uuid4().hex}.wav"
        out_path  = Config.AUDIO_FOLDER / out_name
        timestamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

        logger.info("[DEBUG_TTS] Generating fresh audio for text=%r", text[:80])

        try:
            t0   = _time.perf_counter()
            path = _tts_svc.generate_speech(text, out_name)
            elapsed_ms = int((_time.perf_counter() - t0) * 1000)

            with open(path, 'rb') as f:
                raw = f.read()

            file_hash = hashlib.md5(raw).hexdigest()
            file_size = len(raw)

            logger.info(
                "[DEBUG_TTS] filename=%s  size=%d  hash=%s  voice=%s  model=%s  elapsed_ms=%d",
                out_name, file_size, file_hash, GROQ_TTS_VOICE, GROQ_TTS_MODEL, elapsed_ms
            )
            logger.info("Generated audio hash: %s", file_hash)

            return jsonify({
                'success':    True,
                'filename':   out_name,
                'file_size':  file_size,
                'hash':       file_hash,
                'provider':   'GroqOrpheus',
                'voice':      GROQ_TTS_VOICE,
                'model':      GROQ_TTS_MODEL,
                'format':     GROQ_TTS_FORMAT,
                'elapsed_ms': elapsed_ms,
                'timestamp':  timestamp,
                'text':       text,
                'file_path':  path,
            })
        except Exception as e:
            logger.exception("[DEBUG_TTS] Failed: %s", e)
            report = get_tts_debug_report()
            return jsonify({
                'success': False,
                'error':   str(e),
                'tts_health': report,
            }), 500

    return app
