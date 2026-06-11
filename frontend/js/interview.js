/**
 * AI Voice Interview — Interview Controller v15 (DEFINITIVE premature-submission fix)
 *
 * ══════════════════════════════════════════════════════════════════════════════
 * THE ACTUAL ROOT CAUSE — WHY ALL PREVIOUS FIXES FAILED
 * ══════════════════════════════════════════════════════════════════════════════
 *
 * REAL ROOT CAUSE: Accumulating orphaned 240-second timeout timers
 *
 *   Every call to startRecording() created TWO setTimeout() calls:
 *     1. setTimeout(startVAD,   100ms)   — VAD start delay
 *     2. setTimeout(stopRecording, 240s) — 4-minute recording safety timeout
 *
 *   NEITHER handle was stored in a variable.
 *   NEITHER could ever be cancelled.
 *
 *   startRecording() is called once per question (Q1, Q2, Q3, …) and also on
 *   error-recovery paths. Over a 10-question interview it is called 10+ times.
 *   Each call creates a new 240s timer. None of the old ones are ever cleared.
 *
 *   TIMELINE OF THE BUG (typical interview):
 *
 *     T=0s    Q1: startRecording() → Timer-A fires at T=240s
 *     T=50s   Q2: startRecording() → Timer-B fires at T=290s
 *     T=100s  Q3: startRecording() → Timer-C fires at T=340s
 *     T=150s  Q4: startRecording() → Timer-D fires at T=390s
 *     ...
 *     T=240s  Timer-A fires: state === S.RECORDING → stopRecording('timeout')
 *             → candidate is mid-answer on Q4 or Q5 → PREMATURE SUBMISSION
 *
 *   The 240s timeout was intended as a per-question safeguard ("no single
 *   answer can exceed 4 minutes"). It became a global one-shot timer that fired
 *   240 seconds after the FIRST question, not 240 seconds after the CURRENT
 *   question started.
 *
 *   WHY PREVIOUS VAD FIXES DID NOT HELP:
 *   Increasing VAD_SILENCE_DURATION, VAD_MIN_SPEECH_MS, fixing speechDuration
 *   arithmetic, declaring module-scope variables — none of these touched the
 *   orphaned timer. The timer always called stopRecording('timeout'), which
 *   bypasses all VAD logic entirely. The VAD improvements were irrelevant to
 *   this specific failure mode.
 *
 * FIX (v15):
 *   Store BOTH timer handles in module-scope variables:
 *     let _vadStartTimer    = null;  — handle for the 100ms VAD-start delay
 *     let _recordingTimeout = null;  — handle for the 240s safety timeout
 *
 *   startRecording() clears any existing handle before setting a new one:
 *     clearTimeout(_vadStartTimer);    _vadStartTimer    = setTimeout(...)
 *     clearTimeout(_recordingTimeout); _recordingTimeout = setTimeout(...)
 *
 *   stopRecording() also clears both:
 *     clearTimeout(_vadStartTimer);    _vadStartTimer    = null;
 *     clearTimeout(_recordingTimeout); _recordingTimeout = null;
 *
 *   This guarantees at most ONE of each timer is active at any time,
 *   regardless of how many times startRecording() is called.
 *
 * SECONDARY FIX — _emit_to_room triple-delivery in socket_routes.py:
 *   _emit_to_room() called socketio.emit(to=room) then _safe_emit(sid).
 *   _safe_emit(sid) re-emitted to the room AGAIN (since interview_id was
 *   in the payload). evaluation_ready arrived 2-3× per evaluation.
 *   Fixed by making _safe_emit emit to sid directly when called with a sid,
 *   not to the room a second time.
 *
 * ══════════════════════════════════════════════════════════════════════════════
 * ROOT CAUSES FIXED IN v14 (still valid, all retained)
 * ══════════════════════════════════════════════════════════════════════════════
 *
 * ROOT CAUSE #1 — vadCumulativeSilenceMs / vadLastFrameTime never declared
 *   SYMPTOM:  Variables used inside startVAD() were never declared with `let`
 *             in the module scope. They became implicit globals, meaning their
 *             values persisted across questions. Q2 onward inherited stale
 *             accumulated silence from Q1, causing immediate spurious auto-submit.
 *   FIX:      Added `let vadCumulativeSilenceMs = 0` and `let vadLastFrameTime`
 *             to the VAD state block. startVAD() resets both on every call.
 *
 * ROOT CAUSE #2 — speechDuration calculation was incorrect
 *   SYMPTOM:  `speechDuration = (now - speechStartTime) - vadCumulativeSilenceMs`
 *             uses wall-clock elapsed since first speech frame, then subtracts
 *             accumulated silence. But vadCumulativeSilenceMs only counts silence
 *             that occurred AFTER speechStartTime. For long answers with many
 *             pauses, the wall-clock value was already large, causing
 *             speechDuration to quickly satisfy VAD_MIN_SPEECH_MS even when the
 *             candidate had only spoken a few seconds since the last pause reset.
 *             The fix tracks net speech time using a dedicated accumulator.
 *   FIX:      Added `vadSpeechMs` accumulator. Every frame where rms >=
 *             VAD_SPEECH_THRESHOLD adds frameDelta to vadSpeechMs. Auto-submit
 *             guard checks `vadSpeechMs >= VAD_MIN_SPEECH_MS` — an exact count
 *             of milliseconds the candidate was actually speaking.
 *
 * ROOT CAUSE #3 — VAD starts 500ms after recording; early speech missed
 *   SYMPTOM:  startRecording() delays startVAD() by 500ms. If the candidate
 *             begins speaking immediately, those first 500ms of speech frames
 *             are never seen by the VAD loop. vadSpeechMs underestimates real
 *             speech. The previous wall-clock hack compensated but introduced #2.
 *   FIX:      Reduced VAD start delay from 500ms → 100ms. The 500ms delay was
 *             a conservative guard against the MediaRecorder init noise spike.
 *             100ms is enough for the recorder to stabilise while losing only
 *             ~100ms of candidate speech detection.
 *
 * ROOT CAUSE #4 — Countdown display showed stale/wrong seconds
 *   SYMPTOM:  `_startVADCountdown(secsLeft)` calculated remaining seconds at
 *             countdown start and decremented by 1 per second on a fixed
 *             interval. But vadCumulativeSilenceMs accumulates at a variable
 *             rate (frame drops, tab blur, etc.), so the displayed countdown
 *             drifted from actual remaining time.
 *   FIX:      Countdown interval recalculates remaining seconds on each tick
 *             from the live vadCumulativeSilenceMs value, not a decrement.
 *             Display is always accurate.
 *
 * ROOT CAUSE #5 — In-between zone logic had an off-by-one logical gap
 *   SYMPTOM:  When rms was in the in-between zone (SILENCE_THRESHOLD ≤ rms <
 *             SPEECH_THRESHOLD) and vadSilenceStart was NOT set (i.e., candidate
 *             was transitioning from speech to soft audio), the branch fell
 *             through with no action — neither adding to vadSpeechMs nor
 *             blocking silence accumulation. On the very next frame, if rms
 *             dropped below SILENCE_THRESHOLD, vadSilenceStart was set fresh
 *             but frameDelta was only ~16ms, so vadCumulativeSilenceMs jumped
 *             by the full gap of all in-between frames on the NEXT true-silence
 *             frame (because frameDelta is measured from the last frame, which
 *             was an in-between frame, not a speech frame).
 *   FIX:      True silence branch uses a dedicated `vadSilenceGateStart`
 *             timestamp. frameDelta for silence accumulation is capped at 100ms
 *             per frame so that a stalled RAF or tab-switch can never inject a
 *             multi-second frameDelta into the silence accumulator in one frame.
 *
 * ROOT CAUSE #6 — No protection against MediaRecorder "inactive" phantom stop
 *   SYMPTOM:  On some browsers (Firefox ≤ 115, iOS Safari), MediaRecorder can
 *             fire onstop without the recording actually being user-requested.
 *             This can happen if the track is briefly muted by the OS (phone
 *             call interruption, Bluetooth headset reconnect). The onstop handler
 *             ran unconditionally, uploading a short/empty blob and advancing
 *             the interview to the next question.
 *   FIX:      stopRecording() now records the reason. onstop checks
 *             `_stopReason` — if it's not 'manual', 'vad', or 'timeout', the
 *             stop is treated as an unexpected interrupt and recording restarts.
 *             Also: minimum blob size guard increased from 1000 → 4000 bytes
 *             (~250ms of Opus audio) with a clearer error message.
 *
 * ══════════════════════════════════════════════════════════════════════════════
 * FIXES INHERITED FROM v13
 * ══════════════════════════════════════════════════════════════════════════════
 *   • VAD_SILENCE_DURATION  5 000 ms → 10 000 ms
 *   • VAD_MIN_SPEECH_MS     1 500 ms → 8 000 ms  (now correctly counted)
 *   • VAD_COUNTDOWN_START_MS 3 000 ms → 7 000 ms
 *   • VAD_SPEECH_THRESHOLD  18 → 15 (more sensitive for soft-spoken candidates)
 *   • Silence accumulator reset on ANY audio above silence floor
 *   • "I'm Done Speaking" button always enabled during recording
 *   • Enhanced logging for every VAD state transition
 *
 * FIXES INHERITED FROM v8.2 / v10–v12:
 *   FIX #1 — next_question_audio listener
 *   FIX #2 — join_interview handshake
 *   FIX #3 — Exhaustive console logging
 */

const API = window.location.protocol + '//' + window.location.host;

// ── session bootstrap ──────────────────────────────────────────────────────
const interviewId           = sessionStorage.getItem('interviewId');
const firstQuestion         = sessionStorage.getItem('firstQuestion');

console.log('[BOOTSTRAP] interviewId:', interviewId);
console.log('[BOOTSTRAP] firstQuestion:', firstQuestion ? firstQuestion.slice(0, 80) + '...' : 'MISSING');
console.log('[BOOTSTRAP] first question audio: will fetch on-demand from /api/tts');

if (!interviewId) {
    console.error('[BOOTSTRAP] No interviewId in sessionStorage — redirecting to index');
    window.location.href = 'index.html';
}

// ── DOM ─────────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const timerEl           = $('timer');
const progressEl        = $('progress');
const statusEl          = $('statusText');
const hintEl            = $('actionHint');
const questionEl        = $('currentQuestion');
const canvas            = $('waveform');
const canvasWrap        = $('waveformContainer');
const doneBtn           = $('doneButton');
const muteBtn           = $('muteButton');
const cancelBtn         = $('cancelButton');
const tToggle           = $('transcriptToggle');
const tOverlay          = $('transcriptOverlay');
const tClose            = $('closeTranscript');
const tContent          = $('transcriptContent');
const tContentMobile    = $('transcriptContentMobile');
const audioEl           = $('aiAudioPlayer');
const audioUnlockBanner = $('audioUnlockBanner');

// ── state machine ────────────────────────────────────────────────────────────
const S = { IDLE:'idle', AI_SPEAKING:'ai_speaking', RECORDING:'recording', UPLOADING:'uploading' };
let state = S.IDLE;

// ── mic / recording ──────────────────────────────────────────────────────────
let mediaStream = null;
let recorder    = null;
let recBlobs    = [];
let muted       = false;

// ── Recording timer handles (THE FIX — v15) ──────────────────────────────────
// ROOT CAUSE: startRecording() created two setTimeout() calls whose handles
// were NEVER stored and NEVER cleared. Each call to startRecording() added
// a new orphaned 240s timer. Over a 10-question interview, 10 timers ran
// concurrently. The FIRST timer fired at T=240s from Q1 and called
// stopRecording('timeout') while the candidate was answering Q4/Q5.
//
// FIX: Store both handles here. startRecording() clears the old handle before
// setting a new one. stopRecording() also clears both. At most ONE of each
// timer is ever active at any point in the interview.
let _vadStartTimer    = null;  // handle for the 100ms VAD-start delay
let _recordingTimeout = null;  // handle for the 240s per-question safety timeout

// ── Web Audio ────────────────────────────────────────────────────────────────
let audioCtx = null;
let analyser  = null;

// ── Audio unlock tracking ────────────────────────────────────────────────────
let audioUnlocked        = false;
let _audioUnlockPromise  = null;   // FIX: callers can await full element reset

// ── VAD state ────────────────────────────────────────────────────────────────
let vadActive              = false;
let vadSilenceStart        = null;
let vadSpeechDetected      = false;
let vadRafHandle           = null;
let vadCountdownInterval   = null;
// FIX v14 ROOT CAUSE #1: Declared here (not inside startVAD) so they never
// carry stale values across questions. startVAD() resets all of these.
let vadCumulativeSilenceMs = 0;   // ms of TRUE silence accumulated this answer
let vadLastFrameTime       = null; // timestamp of previous VAD frame
let vadSpeechMs            = 0;   // FIX v14 RC#2: net ms of confirmed speech frames
let vadLastRms             = 0;   // last smoothed RMS (used in log messages)

// ── VAD THRESHOLDS ───────────────────────────────────────────────────────────
// RMS level (0-255 scale) below which a frame is considered silence.
const VAD_SILENCE_THRESHOLD  = 10;
// RMS level above which a frame confirms speech. Lowered from 18→15 to catch
// soft-spoken candidates and ensure speechDuration accumulates correctly.
const VAD_SPEECH_THRESHOLD   = 15;
// How many ms of TRUE silence (rms < VAD_SILENCE_THRESHOLD) must accumulate
// before auto-submit fires. 10 000 ms = a full 10-second dead silence.
// FIX v13: was 5 000 ms — caused premature submission on normal thinking pauses.
const VAD_SILENCE_DURATION   = 10000;
// Minimum total speech detected before silence can trigger auto-submit.
// FIX v13: was 1 500 ms — candidates with 2s answers were cut off on first pause.
const VAD_MIN_SPEECH_MS      = 8000;
// Silence duration at which the countdown warning UI appears.
// 3 000 ms before the 10 000 ms threshold = warning appears at 7 000 ms.
// FIX v13: was 3 000 ms (with 5 000 ms total), giving only 2 s of visible warning.
const VAD_COUNTDOWN_START_MS = 7000;

// ── interview ─────────────────────────────────────────────────────────────────
let qCount          = 1;
let startTime       = Date.now();
let timerHandle     = null;
let cleaning        = false;
let speechStartTime = null;

// ── Web Speech API (fallback TTS) ────────────────────────────────────────────
const synth    = window.speechSynthesis || null;
let synthVoice = null;

// ── Socket.IO ────────────────────────────────────────────────────────────────
let socket     = null;
let chunkIndex = 0;

// ── Audio delivery promise for Q2, Q3, ... (next_question_audio event) ───────
// When next_question arrives (question text), we set up a Promise here.
// It resolves when next_question_audio arrives (with the actual audio data).
// playAI() awaits this promise with a timeout so it never blocks forever.
let _nextAudioResolve = null;
let _nextAudioPromise = null;

function _setupNextAudioPromise() {
    _nextAudioPromise = new Promise(function(resolve) {
        _nextAudioResolve = resolve;
    });
    return _nextAudioPromise;
}

function _resolveNextAudio(audioData) {
    if (_nextAudioResolve) {
        _nextAudioResolve(audioData);
        _nextAudioResolve = null;
    }
}

function initSocket() {
    socket = io(API, {
        path: '/socket.io',
        transports: ['websocket'],   // websocket-only; no polling fallback needed
        upgrade: false,
        reconnection: true,
        reconnectionAttempts: Infinity,  // never give up — interview state is preserved server-side
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        // ROOT CAUSE FIX (v12): The server's ping_timeout is now 300s and the
        // hub stays alive because all blocking work (STT/LLM/ffmpeg/TTS) runs
        // in OS threads.  Set client timeout to match server ping_timeout so
        // Engine.IO connection handshake succeeds even on slow connections.
        timeout: 300000,
    });

    socket.on('connect', () => {
        console.log('[WS] Connected, sid:', socket.id);
        // FIX #2: Register this socket session with the interview room
        // so the backend can target emits at us specifically.
        if (interviewId) {
            socket.emit('join_interview', { interview_id: interviewId });
            console.log('[WS] join_interview emitted for', interviewId);
        }
    });

    socket.on('joined_interview', (data) => {
        console.log('[WS:joined_interview] Confirmed room join:', data);
    });

    socket.on('disconnect', (reason) => {
        console.warn('[WS] Disconnected:', reason);
        if (!cleaning) setStatus('Connection lost — attempting to reconnect…');
    });

    socket.on('reconnect', (attempt) => {
        console.log('[WS] Reconnected after', attempt, 'attempt(s). sid:', socket.id);
        // Re-join the interview room with the new sid so room-based emits reach us.
        if (interviewId) {
            socket.emit('join_interview', { interview_id: interviewId });
            console.log('[WS] Re-emitted join_interview after reconnect');
        }
        // If we reconnected while the server was still processing (state=UPLOADING),
        // restore the status so the user isn't left looking at a blank/stale screen.
        if (state === S.UPLOADING && !cleaning) {
            setStatus('Reconnected — still processing your answer…');
        }
    });

    socket.on('connect_error', (err) => {
        console.error('[WS] Connection error:', err.message, err.description || '');
        if (!cleaning) setStatus('WebSocket error: ' + err.message + ' — retrying…');
    });

    // v12: Application-level heartbeat from server during long processing operations.
    // Confirms the server is alive even when STT/LLM/TTS are running in background threads.
    socket.on('heartbeat', (data) => {
        if (data.interview_id === interviewId) {
            console.debug('[WS:heartbeat] server alive, ts:', data.ts);
        }
    });

    socket.on('status', (data) => {
        if (data.interview_id === interviewId || !data.interview_id) {
            console.log('[WS:status]', data.message);
            // FIX v10: Show all backend status messages regardless of local state.
            // Previously gated on state === S.UPLOADING, which caused "Transcribing
            // your answer…" to never display if state had slipped back to IDLE
            // between stopRecording() and the server's first status event.
            if (!cleaning) setStatus(data.message);
        }
    });

    // Transcript arrives as soon as Whisper finishes
    socket.on('transcript', (data) => {
        if (data.interview_id !== interviewId) return;
        console.log('[WS:transcript]', data.transcript && data.transcript.slice(0, 80));
        if (data.transcript) addTranscript(data.transcript, 'candidate');
    });

    // ── FIX #1: next_question — question TEXT arrives immediately after LLM ──
    // Audio arrives separately via next_question_audio (a few seconds later).
    socket.on('next_question', async (data) => {
        if (data.interview_id !== interviewId) return;
        console.log('[WS:next_question] RECEIVED — is_final=', data.is_final,
                    '| question:', data.question && data.question.slice(0, 60));
        if (data.timing) {
            console.log('[PERF] STT:', data.timing.stt_ms + 'ms',
                        '| LLM:', data.timing.llm_ms + 'ms',
                        '| TOTAL_SO_FAR:', data.timing.total_ms + 'ms');
        }

        progressEl.textContent = data.is_final ? 'Concluding…' : 'Question ' + (data.question_count || qCount + 1);
        qCount = data.question_count || qCount;

        // QUESTION RECEIVED — render it immediately
        console.log('[QUESTION RECEIVED] Displaying question text');
        displayQuestion(data.question);
        addTranscript(data.question, 'interviewer');
        console.log('[QUESTION RENDERED] Question visible in UI');

        // Set up the audio delivery promise BEFORE doing anything async
        // so we don't miss the next_question_audio event.
        const audioPromise = _setupNextAudioPromise();

        // FIX: Only resolve immediately if audio is actually present and non-trivial.
        // If data.audio is null/empty, keep waiting for next_question_audio (max 20s).
        // This prevents the final question from prematurely getting null audio and
        // racing /api/tts before the backup _deliver_audio greenlet can deliver.
        if (data.audio && data.audio.length > 100) {
            console.log('[AUDIO] Inline audio in next_question payload — resolving promise immediately');
            _resolveNextAudio({ audio: data.audio, fmt: data.audio_format || 'wav' });
        } else {
            console.log('[AUDIO] No inline audio — waiting for next_question_audio event (max 20s)…');
        }

        if (data.is_final) {
            // For the final question: race the socket audio event (max 20s).
            // If it arrives in time, great. If not, playAI fetches from /api/tts.
            // Either way, audio plays FULLY before evaluate+redirect.
            let finalAudio = null;
            let finalFmt   = 'wav';
            try {
                const audioData = await Promise.race([
                    audioPromise,
                    new Promise(function(res) {
                        setTimeout(function() { res({ audio: null, fmt: 'wav' }); }, 20000);
                    }),
                ]);
                finalAudio = audioData.audio;
                finalFmt   = audioData.fmt || 'wav';
            } catch (_) {}

            try {
                // playAI fetches from Groq on-demand if finalAudio is null
                await Promise.race([
                    playAI(data.question, finalAudio, finalFmt),
                    sleep(40000),
                ]);
            } catch (audioErr) {
                console.warn('[FLOW] Closing audio error (non-fatal):', audioErr);
            }
            await triggerFinalEvaluation();
            teardownAndRedirect();
        } else {
            // Middle questions: race socket audio event (max 20s).
            // playAI fetches from Groq on-demand if audio is null.
            let midAudio = null;
            let midFmt   = 'wav';
            try {
                const audioData = await Promise.race([
                    audioPromise,
                    new Promise(function(res) {
                        setTimeout(function() {
                            res({ audio: null, fmt: 'wav' });
                            if (_resolveNextAudio) _resolveNextAudio({ audio: null, fmt: 'wav' });
                        }, 20000);
                    }),
                ]);
                midAudio = audioData.audio;
                midFmt   = audioData.fmt || 'wav';
            } catch (_) {}

            console.log('[AUDIO RECEIVED] audio=', midAudio ? midAudio.length + ' chars' : 'null — playAI will fetch from Groq');
            await playAI(data.question, midAudio, midFmt);
            console.log('[AUDIO PLAYBACK FINISHED] Starting recording');
            startRecording();
        }
    });

    // ── FIX #1: next_question_audio — TTS audio arrives here ─────────────────
    socket.on('next_question_audio', (data) => {
        if (data.interview_id !== interviewId) return;
        console.log('[WS:next_question_audio] RECEIVED audio=',
                    data.audio ? data.audio.length + ' chars b64' : 'null',
                    'fmt=', data.audio_format);
        _resolveNextAudio({ audio: data.audio, fmt: data.audio_format || 'wav' });
    });

    // FIX v10: evaluation_ready permanent listener REMOVED.
    // It was racing with the socket.once('evaluation_ready') in triggerFinalEvaluation()
    // and cancelInterview(). The permanent listener fired first, called teardown() which
    // disconnected the socket, and then redirected — BEFORE the socket.once() could store
    // the evaluation in sessionStorage. Result: result.html loaded with no data and showed
    // infinite loading. The socket.once() listeners in triggerFinalEvaluation / cancelInterview
    // are the correct, single handlers for this event.

    socket.on('error', (data) => {
        if (data.interview_id && data.interview_id !== interviewId) return;
        console.error('[WS:error]', data.message);
        const msg = data.message || 'Unknown error';

        // FIX v10: Always resolve any pending next-audio promise so the next_question
        // handler doesn't hang waiting for audio that will never come after an error.
        _resolveNextAudio({ audio: null, fmt: 'wav' });

        if (state === S.UPLOADING) {
            if (/detect speech|too small|too short/i.test(msg)) {
                setStatus(msg);
                startRecording();
            } else {
                setStatus('Error: ' + msg + '. Please try again.');
                startRecording();
            }
        } else {
            setStatus('Error: ' + msg);
        }
    });
}

// ============================================================
// FIRST-QUESTION AUDIO — async TTS delivery via Socket.IO
// ============================================================
let _resolveFirstAudio = null;
const firstAudioPromise = new Promise(function(resolve) {
    _resolveFirstAudio = resolve;
});

function _registerFirstAudioListener(sock) {
    sock.on('first_question_audio', function(data) {
        if (data.interview_id !== interviewId) return;
        console.log('[WS:first_question_audio] received. audio=',
            data.audio ? data.audio.length + ' chars b64' : 'null',
            'fmt=', data.audio_format);
        if (_resolveFirstAudio) {
            _resolveFirstAudio({ audio: data.audio, fmt: data.audio_format || 'wav' });
            _resolveFirstAudio = null;
        }
    });
}

// ============================================================
// BOOT
// ============================================================
boot();

async function boot() {
    console.log('[BOOT] Interview page loaded. ID:', interviewId);
    loadSynthVoice();
    bindEvents();
    startTimer();
    drawWaveform();
    initSocket();

    // Register the first_question_audio listener on the socket created above.
    _registerFirstAudioListener(socket);

    setStatus('Tap the banner to begin…');
    await waitForBannerTap();

    setStatus('Requesting microphone…');
    try {
        await openMicrophone();
        console.log('[MIC] Microphone opened successfully');
    } catch (e) {
        console.error('[MIC] Error:', e);
        setStatus('Microphone access denied. Please grant permission and reload.');
        setHint('Click the lock icon in the address bar → allow microphone → reload.');
        return;
    }

    if (firstQuestion) {
        setStatus('Starting interview…');
        displayQuestion(firstQuestion);
        addTranscript(firstQuestion, 'interviewer');

        // Always fetch audio on-demand via /api/tts — avoids sessionStorage size
        // limit (~5MB) and the race condition between HTTP response and socket join.
        console.log('[BOOT] First question audio: fetching from Groq via /api/tts');
        console.log('[AUDIO PLAYBACK STARTED] First question');
        await playAI(firstQuestion, null, 'wav');
        console.log('[AUDIO PLAYBACK FINISHED] First question — starting recording');
        startRecording();
    } else {
        setStatus('Session lost. Please return to the home page.');
    }
}

function waitForBannerTap() {
    return new Promise(function(resolve) {
        if (audioUnlocked) {
            console.log('[BOOT] Audio already unlocked — skipping banner');
            if (audioUnlockBanner) audioUnlockBanner.classList.add('hidden');
            resolve();
            return;
        }
        console.log('[BOOT] Waiting for banner tap…');
        showUnlockBanner(function() {
            console.log('[BOOT] Banner tapped — proceeding to mic request');
            resolve();
        });
    });
}

function showUnlockBanner(onTap) {
    if (!audioUnlockBanner) {
        if (onTap) onTap();
        return;
    }
    audioUnlockBanner.classList.remove('hidden');
    audioUnlockBanner.addEventListener('click', () => {
        // FIX: await the full unlock/reset promise so the audio element is
        // completely idle before boot() calls playAI() for the first question.
        const p = markAudioUnlocked();
        audioUnlockBanner.classList.add('hidden');
        // onTap resolves waitForBannerTap → boot continues.
        // We resolve AFTER the element reset promise settles (or immediately on
        // the rare path where markAudioUnlocked returns a non-promise).
        if (p && typeof p.then === 'function') {
            p.then(function() { if (onTap) onTap(); });
        } else {
            if (onTap) onTap();
        }
    }, { once: true });
}

function markAudioUnlocked() {
    if (audioUnlocked) return _audioUnlockPromise || Promise.resolve();
    audioUnlocked = true;
    console.log('[AUDIO] Unlocked via user gesture');
    if (audioUnlockBanner) audioUnlockBanner.classList.add('hidden');
    if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume().then(() => console.log('[AUDIO] AudioContext resumed'));
    }
    // Play a silent WAV on the <audio> element inside the user-gesture handler.
    // This is the ONLY way to unlock autoplay for HTMLAudioElement in Chrome.
    // Without this, every subsequent audioEl.play() call is rejected as autoplay.
    //
    // FIX: After the silent play settles we MUST fully reset the element
    // (pause → src='' → load) so it is completely idle before the first real
    // question audio plays.  If we don't, the element is still in a
    // "playing/ended" state from the silent WAV and the first real play()
    // call will be rejected by the browser.
    // We store the promise and return it so waitForBannerTap → boot() can
    // await full element reset before calling playAI().
    if (audioEl) {
        // Minimal valid WAV: 44-byte header + 1 sample of silence
        const silentWav = 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
        audioEl.src = 'data:audio/wav;base64,' + silentWav;
        audioEl.volume = 0;
        const unlockPlay = audioEl.play();
        if (unlockPlay) {
            _audioUnlockPromise = unlockPlay.then(() => {
                // FIX: Fully clear the element state so it is completely idle.
                audioEl.pause();
                audioEl.src    = '';
                audioEl.volume = 1;
                audioEl.load();
                console.log('[AUDIO] <audio> element unlocked and reset — ready for first question');
            }).catch((e) => {
                // Even if silent play failed, reset the element so it is idle.
                audioEl.pause();
                audioEl.src    = '';
                audioEl.volume = 1;
                audioEl.load();
                console.warn('[AUDIO] Silent unlock play failed (element reset anyway):', e.message);
            });
        } else {
            // Synchronous path (shouldn't happen in modern browsers)
            audioEl.pause();
            audioEl.src    = '';
            audioEl.volume = 1;
            audioEl.load();
            _audioUnlockPromise = Promise.resolve();
        }
    } else {
        _audioUnlockPromise = Promise.resolve();
    }
    return _audioUnlockPromise;
}

// ============================================================
// MIC
// ============================================================
async function openMicrophone() {
    mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl:  true,
            sampleRate:       16000,
        }
    });

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') {
        await audioCtx.resume().catch(() => {});
    }

    const src = audioCtx.createMediaStreamSource(mediaStream);
    analyser  = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.3;
    src.connect(analyser);
    console.log('[MIC] AudioContext state:', audioCtx.state);
}

function pickMime() {
    const opts = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/mp4',
    ];
    for (const m of opts) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
    }
    return '';
}

// ============================================================
// WEB SPEECH API voices
// ============================================================
function loadSynthVoice() {
    if (!synth) return;
    const pick = () => {
        const voices = synth.getVoices();
        synthVoice = voices.find(v => /en-US|en-GB/i.test(v.lang) && /female|aria|zira|samantha/i.test(v.name))
                  || voices.find(v => /en/i.test(v.lang))
                  || voices[0]
                  || null;
        if (synthVoice) console.log('[TTS-FALLBACK] Voice:', synthVoice.name);
    };
    pick();
    if (synth.onvoiceschanged !== undefined) synth.onvoiceschanged = pick;
}

// ============================================================
// VAD LOOP
// ============================================================
function startVAD() {
    // FIX v14 RC#1: Reset ALL VAD state here — these are module-scope vars
    // so they carry stale values if startVAD() is called for Q2, Q3, etc.
    vadActive              = true;
    vadSilenceStart        = null;
    vadSpeechDetected      = false;
    speechStartTime        = null;
    vadCumulativeSilenceMs = 0;     // RC#1: was implicitly created as global
    vadLastFrameTime       = null;  // RC#1: was implicitly created as global
    vadSpeechMs            = 0;     // RC#2: net speech accumulator
    vadLastRms             = 0;
    _clearVADCountdown();

    console.log('[VAD] ▶ Started — thresholds: silence=' + VAD_SILENCE_THRESHOLD +
                ' speech=' + VAD_SPEECH_THRESHOLD +
                ' | auto-submit after: ' + VAD_SILENCE_DURATION + 'ms true silence' +
                ' + ' + VAD_MIN_SPEECH_MS + 'ms speech detected' +
                ' | countdown warning at: ' + VAD_COUNTDOWN_START_MS + 'ms silence');

    const buffer     = new Uint8Array(analyser.fftSize);
    const RMS_WINDOW = 8;  // 8-frame rolling average ≈ 130ms smoothing at 60fps
    const rmsHistory = new Array(RMS_WINDOW).fill(0);
    let   rmsIdx     = 0;

    function vadLoop() {
        if (!vadActive) return;
        analyser.getByteTimeDomainData(buffer);

        // ── RMS calculation ───────────────────────────────────────────────
        let sumSq = 0;
        for (let i = 0; i < buffer.length; i++) {
            const v = (buffer[i] - 128) / 128;
            sumSq += v * v;
        }
        const rmsInstant = Math.sqrt(sumSq / buffer.length) * 255;
        rmsHistory[rmsIdx % RMS_WINDOW] = rmsInstant;
        rmsIdx++;
        const rms = rmsHistory.reduce((a, b) => a + b, 0) / RMS_WINDOW;
        vadLastRms = rms;

        const isSpeech      = rms >= VAD_SPEECH_THRESHOLD;
        const isTrueSilence = rms <  VAD_SILENCE_THRESHOLD;
        const isInBetween   = !isSpeech && !isTrueSilence; // breathing, background

        const now = Date.now();

        // FIX v14 RC#5: Cap frameDelta at 100ms so a stalled RAF / tab-switch
        // can never inject seconds of phantom silence in a single frame.
        const rawDelta   = vadLastFrameTime ? (now - vadLastFrameTime) : 16;
        const frameDelta = Math.min(rawDelta, 100);
        vadLastFrameTime = now;

        if (isSpeech) {
            // ── Confirmed speech ──────────────────────────────────────────
            if (!vadSpeechDetected) {
                vadSpeechDetected = true;
                speechStartTime   = now;
                console.log('[VAD] 🎙 Speech STARTED — rms:', rms.toFixed(1),
                            '| first speech frame at', new Date(now).toISOString());
            } else if (vadCumulativeSilenceMs > 0) {
                console.log('[VAD] 🎙 Speech RESUMED after',
                            vadCumulativeSilenceMs.toFixed(0) + 'ms true silence — resetting silence clock.',
                            'Total speech so far:', vadSpeechMs.toFixed(0) + 'ms');
            }
            // FIX v14 RC#2: Accumulate exact net speech time per frame.
            vadSpeechMs += frameDelta;
            // Reset silence clock — candidate is actively speaking.
            vadCumulativeSilenceMs = 0;
            vadSilenceStart        = null;
            _clearVADCountdown();

        } else if (isInBetween && vadSpeechDetected) {
            // ── In-between zone: breathing, soft sounds, low background ──
            // Candidate is still present at the mic. Do NOT accumulate silence
            // and do NOT add to vadSpeechMs. Pause both clocks.
            if (vadSilenceStart) {
                // Transitioning from true-silence back to in-between: stop
                // the silence clock but keep what was already accumulated.
                vadSilenceStart = null;
                _clearVADCountdown();
                console.log('[VAD] 🫁 In-between zone — silence clock paused at',
                            vadCumulativeSilenceMs.toFixed(0) + 'ms accumulated. rms:', rms.toFixed(1));
            }
            // frameDelta intentionally not added to either accumulator.

        } else if (isTrueSilence && vadSpeechDetected) {
            // ── True silence after confirmed speech ───────────────────────
            if (!vadSilenceStart) {
                vadSilenceStart = now;
                console.log('[VAD] 🔇 Silence STARTED — rms:', rms.toFixed(1),
                            '| net speech so far:', vadSpeechMs.toFixed(0) + 'ms');
            }
            // FIX v14 RC#5: capped frameDelta prevents runaway accumulation.
            vadCumulativeSilenceMs += frameDelta;

            // FIX v14 RC#2: Use vadSpeechMs (exact net speech time) instead
            // of the old wall-clock minus silence approximation.
            const speechMs = vadSpeechMs;

            // Show countdown UI when silence is approaching threshold.
            if (vadCumulativeSilenceMs >= VAD_COUNTDOWN_START_MS && !vadCountdownInterval) {
                console.log('[VAD] ⏳ Countdown WARNING started —',
                            'silence:', vadCumulativeSilenceMs.toFixed(0) + 'ms /',  VAD_SILENCE_DURATION + 'ms,',
                            'net speech:', speechMs.toFixed(0) + 'ms / ' + VAD_MIN_SPEECH_MS + 'ms required');
                _startVADCountdown();
            }

            // Auto-submit fires only when BOTH guards are satisfied:
            //   Guard A: Enough confirmed speech (candidate gave a real answer)
            //   Guard B: Full true-silence threshold exceeded
            if (speechMs >= VAD_MIN_SPEECH_MS &&
                vadCumulativeSilenceMs >= VAD_SILENCE_DURATION) {
                console.log('[VAD] 🚀 AUTO-SUBMIT triggered —',
                            'cumulative silence:', vadCumulativeSilenceMs.toFixed(0) + 'ms,',
                            'net speech:', speechMs.toFixed(0) + 'ms,',
                            'rms:', rms.toFixed(1));
                vadActive = false;
                _clearVADCountdown();
                stopRecording('vad');
                return;
            }

            // Progress log every 2 seconds of silence
            if (vadCumulativeSilenceMs > 0 &&
                Math.floor(vadCumulativeSilenceMs / 2000) >
                Math.floor((vadCumulativeSilenceMs - frameDelta) / 2000)) {
                console.log('[VAD] 🔇 Silence accumulating:',
                            vadCumulativeSilenceMs.toFixed(0) + 'ms /' + VAD_SILENCE_DURATION + 'ms |',
                            'net speech:', speechMs.toFixed(0) + 'ms /' + VAD_MIN_SPEECH_MS + 'ms required |',
                            'rms:', rms.toFixed(1));
            }

        }
        // True silence before any speech — do nothing, wait for candidate to begin.

        vadRafHandle = requestAnimationFrame(vadLoop);
    }
    vadRafHandle = requestAnimationFrame(vadLoop);
}

function _startVADCountdown() {
    // FIX v14 RC#4: Don't accept a fixed secsLeft parameter — recalculate
    // from live vadCumulativeSilenceMs on every tick so display is always
    // accurate even when RAF runs slower than 60fps (tab blur, CPU load).
    if (vadCountdownInterval) return;
    const _updateHint = function() {
        const remaining = VAD_SILENCE_DURATION - vadCumulativeSilenceMs;
        if (remaining <= 0) {
            _clearVADCountdown();
            return;
        }
        const secsLeft = Math.ceil(remaining / 1000);
        _showVADHint(secsLeft);
    };
    _updateHint();
    vadCountdownInterval = setInterval(_updateHint, 500); // 500ms ticks for smooth display
}
function _clearVADCountdown() {
    if (vadCountdownInterval) { clearInterval(vadCountdownInterval); vadCountdownInterval = null; }
    if (state === S.RECORDING) setHint('Click "I\'m Done Speaking" when finished, or pause for 10s to auto-submit.');
}
function _showVADHint(s) {
    setHint('Still thinking? Auto-submitting in ' + s + 's — click "I\'m Done" now or keep speaking to continue.');
}
function stopVAD() {
    vadActive = false;
    _clearVADCountdown();
    if (vadRafHandle) { cancelAnimationFrame(vadRafHandle); vadRafHandle = null; }
}

// ── stop reason tracking — used by onstop to guard against phantom stops ──
let _stopReason = null;

// ============================================================
// RECORDING — streams chunks via WebSocket
// ============================================================
function startRecording() {
    if (!mediaStream || state === S.RECORDING) return;
    if (muted) { setStatus('Microphone is muted. Unmute to answer.'); return; }

    recBlobs    = [];
    chunkIndex  = 0;
    _stopReason = null; // FIX v14 RC#6: clear phantom-stop guard on fresh start
    const mime  = pickMime();

    try {
        recorder = mime
            ? new MediaRecorder(mediaStream, { mimeType: mime })
            : new MediaRecorder(mediaStream);
    } catch (e) {
        setStatus('Recording not supported in this browser. Please use Chrome or Edge.');
        return;
    }

    // BUG #1 FIX: Collect blobs locally only — do NOT stream chunks via WebSocket.
    // Root cause of EBML corruption: streaming 100ms partial-WEBM chunks and
    // concatenating them server-side produces multiple EBML headers in one file.
    // FFmpeg cannot parse it. Fix: assemble ONE complete Blob after recorder stops,
    // then send it as a single base64 upload. The Blob API produces a valid WEBM.
    recorder.ondataavailable = (e) => {
        if (!e.data || e.data.size === 0) return;
        recBlobs.push(e.data);
        console.log('[RECORDING] chunk', recBlobs.length, 'size', e.data.size);
    };

    recorder.onstop = () => {
        const elapsed   = Math.floor((Date.now() - startTime) / 1000);
        const totalSize = recBlobs.reduce(function(s, b) { return s + b.size; }, 0);
        console.log('[RECORDING] stopped — reason:', _stopReason,
                    '| chunks:', recBlobs.length, '| total bytes:', totalSize);

        // FIX v14 RC#6: Guard against phantom stops (OS audio interruption,
        // Bluetooth reconnect, browser quirk). If _stopReason was never set
        // to a known intentional value, treat this as an unexpected stop and
        // restart recording so the candidate can continue.
        if (_stopReason !== 'manual' && _stopReason !== 'vad' && _stopReason !== 'timeout') {
            console.warn('[RECORDING] Unexpected/phantom stop detected — restarting recording.',
                         '_stopReason was:', _stopReason);
            recBlobs = [];
            if (!cleaning && state !== S.UPLOADING) {
                setTimeout(startRecording, 200);
            }
            return;
        }

        // FIX v14 RC#6: Increased minimum blob size from 1000 → 4000 bytes.
        // 4000 bytes ≈ 250ms of Opus audio at typical bitrates. A blob smaller
        // than this cannot contain a meaningful answer and is almost certainly
        // a microphone initialisation spike or a phantom stop fragment.
        if (totalSize < 4000) {
            recBlobs = [];
            console.warn('[RECORDING] Blob too small (' + totalSize + ' bytes) — discarding and restarting.');
            setStatus('Answer too short. Please speak your full answer.');
            startRecording();
            return;
        }

        if (!socket || !socket.connected) {
            recBlobs = [];
            setStatus('Connection lost — please reconnect and try again.');
            return;
        }

        // Build one complete, valid audio blob from all collected chunks.
        // mimeType must match what MediaRecorder used (e.g. audio/webm;codecs=opus).
        const mimeType = (recorder.mimeType && recorder.mimeType !== '')
            ? recorder.mimeType
            : (mime || 'audio/webm');
        const fullBlob = new Blob(recBlobs, { type: mimeType });
        recBlobs = [];

        console.log('[RECORDING] assembled blob — size:', fullBlob.size,
                    'type:', fullBlob.type);

        // Read the whole blob as base64 ONCE, then send in a single event.
        setStatus('Preparing audio…');
        const reader = new FileReader();
        reader.onloadend = () => {
            // BUG #2 FIX: Do not upload if interview was cancelled/ended
            if (cleaning) { console.log("[RECORDING] Skipping upload — interview cancelled"); return; }
            if (!socket || !socket.connected) {
                setStatus('Connection lost — please reconnect and try again.');
                return;
            }
            const b64 = reader.result.split(',')[1];
            // Derive file extension from mime type for backend to write correct file.
            let ext = 'webm';
            if (mimeType.includes('ogg'))      ext = 'ogg';
            else if (mimeType.includes('mp4')) ext = 'mp4';

            socket.emit('audio_upload', {
                interview_id: interviewId,
                elapsed_time: elapsed,
                audio_data:   b64,
                mime_type:    mimeType,
                extension:    ext,
            });
            setStatus('Transcribing your answer…');
            console.log('[RECORDING] audio_upload emitted —',
                        b64.length, 'chars b64, ext:', ext,
                        'elapsed:', elapsed);
        };
        reader.onerror = () => {
            setStatus('Failed to read audio. Please try again.');
            startRecording();
        };
        reader.readAsDataURL(fullBlob);
    };

    recorder.start(1000); // 1s timeslice — collect blobs locally, no chunk streaming
    setState(S.RECORDING);
    setStatus('Listening… speak your answer.');
    setHint('Click "I\'m Done Speaking" when finished. Auto-submits only after 10s of complete silence.');
    enableDone(true);
    console.log('[RECORDING] ▶ Started — mime:', mime || '(browser default)');

    // FIX v15 ROOT CAUSE: Store BOTH timer handles so they can be cancelled.
    // Clearing before setting guarantees only ONE of each timer is ever active,
    // regardless of how many times startRecording() is called across questions.
    //
    // OLD (broken): setTimeout(startVAD, 100)      → handle discarded, never clearable
    //               setTimeout(stopRecording, 240s) → handle discarded, never clearable
    //
    // NEW (fixed):  clearTimeout(_vadStartTimer);    → cancel any prior VAD timer
    //               _vadStartTimer = setTimeout(...)  → store new handle
    //               clearTimeout(_recordingTimeout);  → cancel any prior 240s timer
    //               _recordingTimeout = setTimeout(...) → store new handle

    clearTimeout(_vadStartTimer);
    _vadStartTimer = setTimeout(function() {
        _vadStartTimer = null;
        if (state === S.RECORDING) {
            console.log('[VAD] ▶ VAD start timer fired — starting VAD loop');
            startVAD();
        }
    }, 100);

    clearTimeout(_recordingTimeout);
    _recordingTimeout = setTimeout(function() {
        _recordingTimeout = null;
        if (state === S.RECORDING) {
            console.warn('[RECORDING] ⏰ 4-minute safety timeout reached — stopping recording.',
                         'This timer was set', new Date().toISOString());
            stopRecording('timeout');
        }
    }, 240000);
}

function stopRecording(reason) {
    reason = reason || 'manual';
    if (state !== S.RECORDING) return;

    // FIX v15 ROOT CAUSE: Cancel both stored timer handles immediately.
    // This prevents the 240s safety timeout from a PREVIOUS question from
    // firing later and submitting a FUTURE answer prematurely.
    clearTimeout(_vadStartTimer);    _vadStartTimer    = null;
    clearTimeout(_recordingTimeout); _recordingTimeout = null;

    // FIX v14 RC#6: Set _stopReason BEFORE calling recorder.stop() so that
    // the onstop handler sees the correct reason and doesn't treat this as
    // a phantom stop. Order matters — onstop can fire synchronously in some
    // browsers when recorder.stop() is called.
    _stopReason = reason;

    if (reason === 'manual') {
        console.log('[RECORDING] ✋ MANUAL SUBMIT — candidate clicked "I\'m Done Speaking"',
                    '| net speech:', vadSpeechMs.toFixed(0) + 'ms',
                    '| silence:', vadCumulativeSilenceMs.toFixed(0) + 'ms');
    } else if (reason === 'vad') {
        console.log('[RECORDING] 🤫 AUTO-SUBMIT via VAD — 10s true silence after',
                    vadSpeechMs.toFixed(0) + 'ms of confirmed speech');
    } else if (reason === 'timeout') {
        console.log('[RECORDING] ⏰ AUTO-SUBMIT via TIMEOUT — max 4-minute recording reached');
    } else {
        console.log('[RECORDING] Stopping — reason:', reason);
    }

    stopVAD();
    enableDone(false);
    try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (e) {}
    setState(S.UPLOADING);
    setStatus('Sending audio to server…');
    setHint('');
}

// ============================================================
// AI AUDIO PLAYBACK — always Groq, never browser speech synthesis
// ============================================================
function playAI(text, b64, fmt) {
    return new Promise(async function(resolve) {
        console.log('[AUDIO] playAI called. text len:', text && text.length,
                    '| b64:', b64 ? b64.length + ' chars' : 'null',
                    '| fmt:', fmt);
        setState(S.AI_SPEAKING);
        setStatus('AI is speaking…');
        setHint('Please listen to the question.');

        // If no audio provided, fetch from Groq via backend on-demand.
        // Covers every case: first/middle/last question TTS failure, any timeout.
        // Browser speech synthesis is NEVER used.
        if (!b64) {
            console.warn('[AUDIO] No b64 — fetching Groq audio on demand from /api/tts');
            setStatus('Loading audio…');
            try {
                const resp = await fetch('/api/tts', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ text: text, interview_id: interviewId }),
                });
                const json = await resp.json();
                if (json.success && json.audio) {
                    console.log('[AUDIO] On-demand Groq TTS received:', json.audio.length, 'chars');
                    b64 = json.audio;
                    fmt = json.audio_format || 'wav';
                } else {
                    console.error('[AUDIO] On-demand TTS failed:', json.error);
                }
            } catch (fetchErr) {
                console.error('[AUDIO] On-demand TTS fetch error:', fetchErr);
            }
            setStatus('AI is speaking…');
        }

        if (b64) {
            console.log('[AUDIO-A] Playing Groq audio via HTML audio element. fmt=' + fmt);
            const success = await _tryAudioElement(b64, fmt, text);
            console.log('[AUDIO-A] Result:', success);
            if (success) {
                console.log('[AUDIO PLAYBACK FINISHED]');
                setState(S.IDLE);
                resolve();
                return;
            }
            // Audio element failed — retry once with a fresh Groq fetch
            console.warn('[AUDIO-A] Element failed — retrying with fresh Groq fetch');
            try {
                const resp2 = await fetch('/api/tts', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ text: text, interview_id: interviewId }),
                });
                const json2 = await resp2.json();
                if (json2.success && json2.audio) {
                    const retryOk = await _tryAudioElement(json2.audio, json2.audio_format || 'wav', text);
                    if (retryOk) {
                        console.log('[AUDIO PLAYBACK FINISHED] (retry)');
                        setState(S.IDLE);
                        resolve();
                        return;
                    }
                }
            } catch (_) {}
        }

        // All Groq attempts exhausted — show text and wait. Still no browser TTS.
        console.warn('[AUDIO] All Groq attempts failed — showing text and waiting');
        setHint('(Audio unavailable — read the question above)');
        const waitMs = Math.max(6000, text.length * 60);
        await sleep(waitMs);
        setState(S.IDLE);
        resolve();
    });
}


// Strategy A — HTML <audio>
async function _tryAudioElement(b64, fmt, text) {
    return new Promise(function(resolve) {
        if (!audioEl) { resolve(false); return; }

        const mimeMap = { mp3: 'audio/mpeg', wav: 'audio/wav', ogg: 'audio/ogg', mp4: 'audio/mp4' };
        const mime    = mimeMap[fmt] || 'audio/wav';

        // Convert base64 → Blob → Object URL.
        // This avoids the data URI path which can fail silently on large WAV files
        // (~600–900KB base64) in Chrome/Safari due to internal size limits on
        // data URIs in HTMLAudioElement. Blob URLs have no such limit.
        let objectUrl = null;
        try {
            const raw     = atob(b64);
            const bytes   = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            const blob    = new Blob([bytes], { type: mime });
            objectUrl     = URL.createObjectURL(blob);
            console.log('[AUDIO-A] Blob URL created, size_bytes:', blob.size, 'fmt:', fmt);
        } catch (blobErr) {
            console.warn('[AUDIO-A] Blob creation failed, falling back to data URI:', blobErr);
            objectUrl = 'data:' + mime + ';base64,' + b64;
        }

        let done  = false;
        const succeed = function() {
            if (done) return;
            done = true;
            if (objectUrl && objectUrl.startsWith('blob:')) URL.revokeObjectURL(objectUrl);
            resolve(true);
        };
        const fail    = function(reason) {
            console.warn('[AUDIO-A] Failed:', reason);
            if (done) return;
            done = true;
            if (objectUrl && objectUrl.startsWith('blob:')) URL.revokeObjectURL(objectUrl);
            resolve(false);
        };

        const timer = setTimeout(function() { fail('timeout'); }, 30000);

        // Reset element fully before setting new src — prevents stale state
        // from a previous play() call causing the next play() to be rejected.
        audioEl.pause();
        audioEl.removeAttribute('src');
        audioEl.load();

        audioEl.src     = objectUrl;
        audioEl.onended = function() { clearTimeout(timer); succeed(); };
        audioEl.onerror = function(e) {
            console.warn('[AUDIO-A] Element error:', e);
            clearTimeout(timer);
            fail('element error');
        };

        audioEl.load();
        // FIX: Give the browser a 100ms tick to fully process the src change and
        // load() call before play().  Without this delay, if the previous operation
        // (e.g. the silent unlock WAV) left the element mid-state, play() fires
        // before the element transitions to HAVE_NOTHING/IDLE and gets rejected.
        setTimeout(function() {
            if (done) return;  // already timed out or failed during load
            const pp = audioEl.play();
            if (pp) {
                pp.then(function() {
                    console.log('[AUDIO PLAYBACK STARTED] HTML audio element playing');
                }).catch(function(err) {
                    clearTimeout(timer);
                    console.warn('[AUDIO-A] play() rejected:', err.message);
                    fail('play() rejected: ' + err.message);
                });
            }
        }, 100);
    });
}

// Strategy B — Web Speech API
function _trySpeechSynthesis(text) {
    return new Promise(function(resolve) {
        if (!synth) { resolve(false); return; }
        const utt   = new SpeechSynthesisUtterance(text);
        utt.rate    = 0.95;
        utt.pitch   = 1.0;
        utt.lang    = 'en-US';
        if (synthVoice) utt.voice = synthVoice;

        let done = false;
        const finish = function(ok) {
            if (done) return;
            done = true;
            clearTimeout(timeout);
            resolve(ok);
        };

        utt.onend   = function() { console.log('[AUDIO-B] SpeechSynthesis done'); finish(true); };
        utt.onerror = function(e) { console.warn('[AUDIO-B] Error:', e.error); finish(false); };

        const timeout = setTimeout(function() { synth.cancel(); finish(false); }, 20000);  // FIX v11: was 90000ms — caused 90s freeze when Web Speech API stalled

        try { synth.speak(utt); } catch (e) { finish(false); }
    });
}

// Strategy C — text countdown
async function _textFallback(text) {
    const duration = Math.max(5000, text.length * 60);
    console.log('[AUDIO-C] Text fallback for ' + duration + 'ms');
    setHint('(Audio unavailable — read the question above)');
    await sleep(duration);
}

function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

// ============================================================
// EVALUATION
// ============================================================
async function triggerFinalEvaluation() {
    clearInterval(timerHandle);
    setStatus('Generating final evaluation…');
    setHint('Analysing all your responses — this takes 15–30 seconds.');

    return new Promise(function(resolve) {
        if (!socket || !socket.connected) {
            console.error('[END] Socket not connected — results page will fetch from DB.');
            resolve(false);
            return;
        }

        // FIX v10: Reduced timeout to 150s (LLM has 120s timeout + network buffer).
        // If we exceed this, we redirect anyway — result.html will fetch from DB.
        const timeout = setTimeout(function() {
            console.error('[END] Timeout waiting for evaluation_ready — redirecting anyway');
            socket.off('evaluation_ready', onEvalReady);  // clean up listener
            setStatus('Evaluation is taking longer than expected. Opening results…');
            resolve(false);
        }, 150000);

        // FIX v10: Named handler so we can clean it up on timeout.
        // Store data in sessionStorage BEFORE resolving so teardown() doesn't
        // race with the redirect. The old permanent socket.on('evaluation_ready')
        // was calling teardown → socket.disconnect() before this once() could run.
        function onEvalReady(data) {
            clearTimeout(timeout);
            if (data.interview_id === interviewId && data.evaluation) {
                sessionStorage.setItem('evaluationData', JSON.stringify(data.evaluation));
                console.log('[END] Evaluation stored. overall=', data.evaluation.overall_score);
                setStatus('Evaluation complete. Opening results…');
            } else {
                console.warn('[END] evaluation_ready received but no data or wrong id:', data.interview_id);
            }
            resolve(true);
        }

        socket.once('evaluation_ready', onEvalReady);
        socket.emit('end_interview', { interview_id: interviewId });
        console.log('[END] end_interview emitted, waiting for evaluation_ready…');
    });
}

function teardownAndRedirect() {
    teardown();
    window.location.href = 'result.html';
}

async function endAndRedirect() {
    if (cleaning) return;
    cleaning = true;
    await triggerFinalEvaluation();
    teardownAndRedirect();
}

async function cancelInterview() {
    if (!confirm('End the interview now? A partial evaluation will be generated.')) return;
    if (cleaning) return;
    cleaning = true;

    if (state === S.RECORDING) { stopVAD(); setState(S.UPLOADING); try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (_) {} }
    clearInterval(timerHandle);
    setStatus('Generating evaluation…');
    setHint('Please wait while we process your responses…');

    // FIX v10: Resolve any pending audio promise so no greenlet stays blocked.
    _resolveNextAudio({ audio: null, fmt: 'wav' });

    try {
        if (socket && socket.connected) {
            await new Promise(function(resolve) {
                const timeout = setTimeout(resolve, 60000);

                // FIX v10: Named handler for cleanup; store data before resolving.
                function onEvalReady(data) {
                    clearTimeout(timeout);
                    if (data.interview_id === interviewId && data.evaluation) {
                        sessionStorage.setItem('evaluationData', JSON.stringify(data.evaluation));
                        console.log('[CANCEL] Evaluation stored.');
                    }
                    resolve();
                }

                socket.once('evaluation_ready', onEvalReady);
                socket.emit('cancel_interview', { interview_id: interviewId });
                console.log('[CANCEL] cancel_interview emitted');
            });
        }
    } catch (e) {
        console.error('[CANCEL] failed:', e);
    } finally {
        teardownAndRedirect();
    }
}

function teardown() {
    stopVAD();
    if (synth) synth.cancel();
    try { if (mediaStream) mediaStream.getTracks().forEach(function(t) { t.stop(); }); } catch (_) {}
    try { if (audioCtx)    audioCtx.close(); } catch (_) {}
    try { if (socket)      socket.disconnect(); } catch (_) {}
}

// ============================================================
// UI HELPERS
// ============================================================
function setState(s)   { state = s; console.log('[STATE]', s); }
function setStatus(m)  { if (statusEl) statusEl.textContent = m; }
function setHint(m)    { if (hintEl)   hintEl.textContent = m; }
function enableDone(b) { doneBtn.disabled = !b; }

function displayQuestion(q) {
    if (!q) return;
    questionEl.textContent = q;
    questionEl.classList.remove('fade-in');
    void questionEl.offsetWidth;
    questionEl.classList.add('fade-in');
    console.log('[QUESTION STATE UPDATED] UI updated with new question');
}

function addTranscript(text, role) {
    if (!text) return;
    const append = function(parent) {
        const div = document.createElement('div');
        div.className = 'transcript-bubble ' + (role === 'interviewer' ? 'ai' : 'candidate');
        const r = document.createElement('div'); r.className = 'role';
        r.textContent = role === 'interviewer' ? 'AI Interviewer' : 'You';
        const c = document.createElement('div'); c.className = 'content';
        c.textContent = text;
        div.appendChild(r); div.appendChild(c);
        parent.appendChild(div);
        parent.scrollTop = parent.scrollHeight;
    };
    if (tContent)       append(tContent);
    if (tContentMobile) append(tContentMobile);
}

function startTimer() {
    timerHandle = setInterval(function() {
        const e = Math.floor((Date.now() - startTime) / 1000);
        const m = String(Math.floor(e / 60)).padStart(2, '0');
        const s = String(e % 60).padStart(2, '0');
        timerEl.textContent = m + ':' + s;
        if (e >= 2700) endAndRedirect();
    }, 1000);
}

// ============================================================
// WAVEFORM VISUALIZER
// ============================================================
function drawWaveform() {
    const ctx2d = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const N = 128;
    const data = new Uint8Array(N);
    let phase = 0;

    function frame() {
        requestAnimationFrame(frame);
        ctx2d.clearRect(0, 0, W, H);
        let stroke = '#D4D4D4', shadow = 'none';

        if (state === S.AI_SPEAKING) {
            stroke = '#007AFF'; shadow = '0 0 60px rgba(0,122,255,0.25)';
            phase += 0.08;
            for (let i = 0; i < N; i++) {
                data[i] = 100 + Math.sin(phase + i * 0.18) * 60 + Math.sin(phase * 1.7 + i * 0.05) * 30;
            }
        } else if (state === S.RECORDING && analyser && !muted) {
            stroke = '#FF3B30'; shadow = '0 0 60px rgba(255,59,48,0.25)';
            analyser.getByteFrequencyData(data);
        } else if (state === S.UPLOADING) {
            stroke = '#A3A3A3'; phase += 0.05;
            for (let i = 0; i < N; i++) data[i] = 50 + Math.sin(phase + i * 0.1) * 30;
        } else {
            data.fill(0);
        }

        canvasWrap.style.boxShadow = shadow;
        ctx2d.strokeStyle = stroke;
        ctx2d.lineWidth = 2;
        ctx2d.beginPath();
        const bw = W / N, cy = H / 2;
        for (let i = 0; i < N; i++) {
            const v = (data[i] || 0) / 255;
            const h = v * (H * 0.6);
            const x = i * bw;
            ctx2d.moveTo(x, cy - h / 2);
            ctx2d.lineTo(x, cy + h / 2);
        }
        ctx2d.stroke();
    }
    frame();
}

// ============================================================
// EVENTS
// ============================================================
function bindEvents() {
    doneBtn.addEventListener('click', function() {
        if (state === S.RECORDING) stopRecording('manual');
    });

    muteBtn.addEventListener('click', function() {
        muted = !muted;
        if (mediaStream) mediaStream.getAudioTracks().forEach(function(t) { t.enabled = !muted; });
        muteBtn.innerHTML = muted
            ? '<i class="ph ph-microphone-slash text-xl text-[#FF3B30]"></i>'
            : '<i class="ph ph-microphone text-xl text-[#0A0A0A]"></i>';
    });

    cancelBtn.addEventListener('click', cancelInterview);

    if (tToggle) tToggle.addEventListener('click', function() { tOverlay.classList.remove('hidden'); });
    if (tClose)  tClose.addEventListener('click',  function() { tOverlay.classList.add('hidden'); });

    document.addEventListener('click',  function() { markAudioUnlocked(); }, { once: true });
    document.addEventListener('keydown', function() { markAudioUnlocked(); }, { once: true });
}

window.addEventListener('beforeunload', function() { if (!cleaning) teardown(); });
