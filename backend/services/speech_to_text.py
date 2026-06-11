import logging
import os
import time
from pathlib import Path

from groq import Groq, AuthenticationError, APIConnectionError, APIStatusError

logger = logging.getLogger(__name__)

GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

_groq_client: Groq | None = None


def _get_groq_client() -> Groq:
    """
    Lazily construct and cache the Groq client.

    Using a module-level singleton means:
      - Only one HTTPS connection pool is created per process.
      - No overhead re-constructing the client on every transcription call.
      - Thread-safe: CPython's GIL protects the simple None check / assignment.
    """
    global _groq_client
    if _groq_client is None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            logger.error(
                "[STT] GROQ_API_KEY environment variable is not set. "
                "Set it in your .env file or shell before starting the server."
            )
        _groq_client = Groq(api_key=api_key or None)
        logger.info("[STT] Groq client initialised (model=%s)", GROQ_WHISPER_MODEL)
    return _groq_client


def transcribe_audio(audio_path: str, language: str = "en") -> str:
    """
    Transcribe an audio file using the Groq Whisper API.

    Takes an audio file path and returns the transcribed text.
    Returns an empty string on any error so callers never need to handle
    exceptions from this function.

    Supported formats (accepted natively by Groq — no ffmpeg pre-conversion needed):
      flac, mp3, mp4, mpeg, mpga, m4a, ogg, opus, wav, webm

    Args:
        audio_path: Absolute or relative path to the audio file.
        language:   BCP-47 language code (default "en").  Providing an explicit
                    language skips Groq's auto-detection step and reduces latency.

    Returns:
        Transcribed text string, or "" if transcription fails.
    """
    t0 = time.perf_counter()
    path = Path(audio_path)

    if not path.exists():
        logger.error("[STT] Audio file not found: %s", audio_path)
        return ""

    file_size = path.stat().st_size
    if file_size == 0:
        logger.error("[STT] Audio file is empty (0 bytes): %s", audio_path)
        return ""

    logger.info("[STT] Transcribing %s (%d bytes) via Groq …", path.name, file_size)

    try:
        client = _get_groq_client()

        with open(audio_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                file=(path.name, audio_file.read()),
                model=GROQ_WHISPER_MODEL,
                response_format="json",
                language=language,
            )

        transcript = (transcription.text or "").strip()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "[STT] Done in %dms → %d chars: '%s'",
            elapsed_ms, len(transcript), transcript[:80],
        )
        return transcript


    except AuthenticationError as e:
        logger.error(
            "[STT] Groq authentication failed — check GROQ_API_KEY. "
            "Details: %s", e
        )
        return ""

    except APIConnectionError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            "[STT] Network error reaching Groq after %dms — "
            "check internet connectivity. Details: %s", elapsed_ms, e
        )
        return ""

    except APIStatusError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            "[STT] Groq API error %d after %dms — %s",
            e.status_code, elapsed_ms, e.message
        )
        return ""

    except FileNotFoundError:
        logger.error("[STT] Audio file disappeared before upload: %s", audio_path)
        return ""

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "[STT] Unexpected error after %dms transcribing %s: %s",
            elapsed_ms, path.name, e
        )
        return ""


class SpeechToTextService:
    """
    Thin wrapper around transcribe_audio() that preserves the class-based
    interface expected by socket_routes.py.

    The heavy lifting (Groq client, error handling, logging) lives in
    transcribe_audio() so it can also be called directly if needed.
    """

    def transcribe(self, audio_path: str, language: str = "en") -> str:
        """
        Transcribe audio file to text via Groq Whisper API.

        Interface is identical to the old local-Whisper version, so no
        call-sites in socket_routes.py need to change.

        Returns:
            Transcribed text, or "" on failure.

        Note:
            Unlike the old implementation this method does NOT raise
            exceptions — all errors are caught inside transcribe_audio()
            and logged.  Callers that previously wrapped this in try/except
            will continue to work correctly (the except block simply won't
            trigger on API failures).
        """
        return transcribe_audio(audio_path, language=language)


stt_service = SpeechToTextService()
