import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from groq import Groq, AuthenticationError, APIConnectionError, APIStatusError
    _GROQ_SDK_AVAILABLE = True
except ImportError:
    _GROQ_SDK_AVAILABLE = False
    AuthenticationError = APIConnectionError = APIStatusError = Exception
    logger.critical(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║  [TTS] FATAL — groq SDK not installed                       ║\n"
        "║  Run:  pip install groq>=0.9.0                              ║\n"
        "║  Then restart the server.                                   ║\n"
        "║  Until then ALL TTS calls will fail and the frontend        ║\n"
        "║  will fall back to the browser's built-in speechSynthesis.  ║\n"
        "╚══════════════════════════════════════════════════════════════╝"
    )

from config.settings import Config

GROQ_TTS_MODEL  = "canopylabs/orpheus-v1-english"

GROQ_TTS_VOICE  = os.getenv("GROQ_TTS_VOICE", "daniel").strip()

GROQ_TTS_MAX_CHARS = 1800

GROQ_TTS_FORMAT = "wav"

_groq_client: "Groq | None" = None


def _get_groq_client() -> "Groq":
    """
    Lazily construct and cache the Groq client.
    One client per process — the SDK manages its own HTTPS connection pool.
    """
    global _groq_client
    if _groq_client is None:
        if not _GROQ_SDK_AVAILABLE:
            raise RuntimeError(
                "groq SDK is not installed. Run: pip install groq>=0.9.0"
            )
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY environment variable is not set. "
                "Add it to backend/.env:  GROQ_API_KEY=gsk_..."
            )
        _groq_client = Groq(api_key=api_key)
        logger.info(
            "[TTS] ✓ Groq client ready  provider=GroqOrpheus  model=%s  voice=%s  fmt=%s",
            GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT
        )
    return _groq_client


def get_tts_debug_report() -> dict:
    """
    Return a dict describing the active TTS configuration.
    Used by /api/tts/health and logged at startup.

    Returns:
        {
          "provider":       "GroqOrpheus",
          "sdk_installed":  True,
          "api_key_set":    True,
          "model":          "canopylabs/orpheus-v1-english",
          "voice":          "leah",
          "output_format":  "wav",
          "client_ready":   True,
          "available_voices": [...],
          "status":         "ok" | "degraded" | "error",
          "error":          null | "...",
        }
    """
    api_key_set  = bool(os.getenv("GROQ_API_KEY", "").strip())
    client_ready = _groq_client is not None

    status = "ok"
    error  = None
    if not _GROQ_SDK_AVAILABLE:
        status = "error"
        error  = "groq SDK not installed — run: pip install groq>=0.9.0"
    elif not api_key_set:
        status = "error"
        error  = "GROQ_API_KEY not set in environment / .env"
    elif not client_ready:
        status = "degraded"
        error  = "Client not yet initialised (will init on first TTS call)"

    return {
        "provider":         "GroqOrpheus",
        "sdk_installed":    _GROQ_SDK_AVAILABLE,
        "api_key_set":      api_key_set,
        "model":            GROQ_TTS_MODEL,
        "voice":            GROQ_TTS_VOICE,
        "output_format":    GROQ_TTS_FORMAT,
        "client_ready":     client_ready,
        "available_voices": ["autumn", "diana", "hannah", "austin", "daniel", "troy"],
        "status":           status,
        "error":            error,
    }


import re
import struct


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """
    Split text into chunks of at most max_chars characters.
    Tries to split on sentence boundaries (. ! ?) first, then on commas/spaces,
    then hard-cuts if no good boundary is found.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        match = None
        for pattern in [r'[.!?]\s', r'[,;]\s', r'\s']:
            matches = list(re.finditer(pattern, window))
            if matches:
                match = matches[-1]
                break

        if match:
            cut = match.start() + 1
        else:
            cut = max_chars

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining.strip())

    return [c for c in chunks if c]


def _find_wav_data_chunk(wav_bytes: bytes) -> tuple[int, int]:
    """
    Parse a WAV/RIFF file and return (data_offset, data_size).

    WAV is a RIFF container: a sequence of tagged chunks. The PCM audio lives
    in the 'data' chunk, which may NOT start at byte 44. Common reasons:
      - Extended fmt chunk  (fmt chunk size = 18 or 40, not 16)
      - Extra chunks before 'data' (e.g. 'fact', 'LIST', 'bext')
      - Groq Orpheus specifically inserts a 'fact' chunk → header is 58+ bytes

    Hard-coding HEADER_SIZE = 44 works only for the minimal-PCM case and
    produces a corrupted WAV whenever any of the above applies.

    Args:
        wav_bytes: Raw bytes of a complete WAV file.

    Returns:
        (data_start, data_size) where data_start is the byte offset of the
        first PCM sample and data_size is the size field from the chunk header.

    Raises:
        ValueError: if the file is not a valid RIFF/WAVE or has no 'data' chunk.
    """
    if len(wav_bytes) < 12:
        raise ValueError("WAV too short to contain a RIFF header")
    if wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        raise ValueError(f"Not a RIFF/WAVE file (magic={wav_bytes[0:4]!r})")

    offset = 12
    while offset + 8 <= len(wav_bytes):
        chunk_id   = wav_bytes[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        if chunk_id == b"data":
            return offset + 8, chunk_size
        offset += 8 + chunk_size + (chunk_size & 1)

    raise ValueError("WAV file contains no 'data' chunk")


def _concat_wav_chunks(wav_parts: list[bytes]) -> bytes:
    """
    Concatenate multiple WAV byte strings into a single valid WAV file.
    All parts must share the same audio format (sample rate, channels, bit depth).

    Properly parses each WAV's RIFF structure to locate the 'data' chunk instead
    of assuming a fixed 44-byte header.  Groq Orpheus (and many other encoders)
    can produce WAV files with 'fact' or other extra chunks, making the data
    offset larger than 44 bytes.  The old hard-coded HEADER_SIZE = 44 sliced
    into the PCM payload, producing a file with a corrupted RIFF header that
    all browsers reject immediately with a MediaError.
    """
    if len(wav_parts) == 1:
        return wav_parts[0]

    pcm_parts: list[bytes] = []
    for i, part in enumerate(wav_parts):
        try:
            data_start, data_size = _find_wav_data_chunk(part)
        except ValueError as exc:
            logger.error("[TTS] _concat_wav_chunks: chunk %d/%d invalid WAV — %s",
                         i + 1, len(wav_parts), exc)
            raise
        pcm_parts.append(part[data_start: data_start + data_size])

    total_pcm      = b"".join(pcm_parts)
    total_pcm_size = len(total_pcm)

    first = wav_parts[0]

    fmt_payload = b""
    offset = 12
    while offset + 8 <= len(first):
        cid  = first[offset:offset + 4]
        csz  = struct.unpack_from("<I", first, offset + 4)[0]
        if cid == b"fmt ":
            fmt_payload = first[offset + 8: offset + 8 + csz]
            break
        offset += 8 + csz + (csz & 1)

    if not fmt_payload:
        raise ValueError("WAV first chunk has no 'fmt ' sub-chunk")

    fmt_chunk  = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    data_chunk = b"data" + struct.pack("<I", total_pcm_size) + total_pcm
    riff_body  = b"WAVE" + fmt_chunk + data_chunk
    riff_header = b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body

    return riff_header


def text_to_speech(text: str, output_file: str) -> "str | None":
    """
    Convert text to speech using Groq Orpheus. Save audio to output_file.
    Return the full output file path, or None on any failure.

    Args:
        text:        Text to synthesise (any length).
        output_file: Filename (not path) for the audio, e.g. "ai_abc.wav".
                     Written to Config.AUDIO_FOLDER / output_file.

    Returns:
        Absolute path string to the written WAV file, or None on error.

    Gevent compatibility:
        The Groq SDK uses httpx in synchronous mode.  httpx makes blocking
        socket calls that gevent monkey-patches into cooperative green sockets.
        No asyncio loop, no threadpool wrapper, no gevent lock needed.
        This call yields to the gevent hub while waiting for the HTTP response.
    """
    t0 = time.perf_counter()

    if not text or not text.strip():
        logger.error("[TTS] Rejected empty text — returning None")
        return None

    output_path = Config.AUDIO_FOLDER / output_file

    logger.info(
        "[TTS_PROVIDER] provider=GroqOrpheus  model=%s  voice=%s  fmt=%s  "
        "chars=%d  output=%s",
        GROQ_TTS_MODEL, GROQ_TTS_VOICE, GROQ_TTS_FORMAT,
        len(text.strip()), output_file,
    )

    try:
        client = _get_groq_client()
        clean_text = text.strip()

        chunks = _chunk_text(clean_text, GROQ_TTS_MAX_CHARS)
        logger.info("[TTS] Synthesising %d chunk(s) for %d chars", len(chunks), len(clean_text))

        raw_audio_parts: list[bytes] = []
        for i, chunk in enumerate(chunks):
            response = client.audio.speech.create(
                model=GROQ_TTS_MODEL,
                voice=GROQ_TTS_VOICE,
                input=chunk,
                response_format=GROQ_TTS_FORMAT,
            )
            part_bytes = response.read()
            if len(part_bytes) < 44:
                logger.error("[TTS] Chunk %d/%d returned suspiciously small audio (%d bytes)",
                             i + 1, len(chunks), len(part_bytes))
                return None
            raw_audio_parts.append(part_bytes)
            logger.debug("[TTS] Chunk %d/%d done  bytes=%d", i + 1, len(chunks), len(part_bytes))

        final_audio = _concat_wav_chunks(raw_audio_parts)

        output_path.write_bytes(final_audio)

        if not output_path.exists():
            logger.error("[TTS] write_to_file() returned but file missing: %s", output_path)
            return None

        file_size  = output_path.stat().st_size
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if file_size < 200:
            logger.error(
                "[TTS] File too small (%d bytes) — Groq returned bad audio. "
                "file=%s  elapsed=%dms", file_size, output_file, elapsed_ms
            )
            try:
                output_path.unlink()
            except OSError:
                pass
            return None

        logger.info(
            "[TTS_REPORT] ✓  provider=GroqOrpheus  voice=%s  model=%s  "
            "elapsed_ms=%d  size_kb=%d  format=%s  file=%s  "
            "timestamp=%s",
            GROQ_TTS_VOICE, GROQ_TTS_MODEL,
            elapsed_ms, file_size // 1024, GROQ_TTS_FORMAT,
            output_file,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        return str(output_path)


    except RuntimeError as e:
        logger.critical("[TTS] ✗ Configuration error — %s", e)
        raise

    except AuthenticationError as e:
        msg = f"401 Authentication failed — GROQ_API_KEY is invalid or revoked. Details: {e}"
        logger.error("[TTS] ✗ %s  key_prefix=%s...", msg, os.getenv("GROQ_API_KEY", "")[:8])
        raise RuntimeError(msg) from e

    except APIConnectionError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        msg = f"Network error after {elapsed_ms}ms — cannot reach api.groq.com. Details: {e}"
        logger.error("[TTS] ✗ %s", msg)
        raise RuntimeError(msg) from e

    except APIStatusError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        body = getattr(e, "body", None) or getattr(e, "message", str(e))
        msg = f"Groq API HTTP {e.status_code} after {elapsed_ms}ms — {body}"
        logger.error("[TTS] ✗ %s", msg)
        raise RuntimeError(msg) from e

    except OSError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        msg = f"File write error after {elapsed_ms}ms — path={output_path} — {e}"
        logger.error("[TTS] ✗ %s", msg)
        raise RuntimeError(msg) from e

    except RuntimeError:
        raise

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        msg = f"Unexpected error after {elapsed_ms}ms — '{text[:40]}...' — {type(e).__name__}: {e}"
        logger.exception("[TTS] ✗ %s", msg)
        raise RuntimeError(msg) from e


class TextToSpeechService:
    """Thin wrapper around text_to_speech() preserving the class interface."""

    def generate_speech(self, text: str, output_filename: str) -> str:
        """
        Generate TTS audio via Groq Orpheus. Return the audio file path.
        Raises RuntimeError with the actual Groq error message on failure.
        """
        return text_to_speech(text, output_filename)


tts_service = TextToSpeechService()


def _startup_tts_check() -> None:
    """
    Run at module import time.  Logs the TTS configuration and catches
    common misconfigurations (missing SDK, missing API key) immediately
    rather than silently failing on the first interview question.
    """
    report = get_tts_debug_report()
    if report["status"] == "ok" or report["status"] == "degraded":
        logger.info(
            "[TTS_STARTUP] provider=%s  model=%s  voice=%s  fmt=%s  "
            "sdk=%s  key_set=%s  status=%s",
            report["provider"], report["model"], report["voice"],
            report["output_format"], report["sdk_installed"],
            report["api_key_set"], report["status"],
        )
        if report["status"] == "degraded":
            logger.warning("[TTS_STARTUP] %s", report["error"])
    else:
        logger.critical(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  [TTS_STARTUP] TTS IS NOT FUNCTIONAL                        ║\n"
            "║  Status: %-51s║\n"
            "║  Error:  %-51s║\n"
            "║  All interview questions will use browser speechSynthesis.  ║\n"
            "║  Fix the issue above and restart the server.                ║\n"
            "╚══════════════════════════════════════════════════════════════╝",
            report["status"], (report["error"] or "")[:51],
        )


_startup_tts_check()

if _GROQ_SDK_AVAILABLE and os.getenv("GROQ_API_KEY", "").strip():
    try:
        _get_groq_client()
        logger.info("[TTS] ✓ Groq client pre-initialized at startup")
    except Exception as _e:
        logger.critical("[TTS] ✗ Groq client pre-init failed at startup: %s", _e)
