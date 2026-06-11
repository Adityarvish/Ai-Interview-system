import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_emb_lock     = threading.Lock()
_emb_instance = None
_emb_load_ms  = 0

def get_embeddings():
    """Return (and cache) the HuggingFaceEmbeddings instance."""
    global _emb_instance, _emb_load_ms
    if _emb_instance is not None:
        return _emb_instance
    with _emb_lock:
        if _emb_instance is None:
            from langchain_community.embeddings import HuggingFaceEmbeddings
            t0 = time.perf_counter()
            logger.info("[CACHE] Loading HuggingFaceEmbeddings (all-MiniLM-L6-v2)…")
            _emb_instance = HuggingFaceEmbeddings(
                model_name="sentence-transformers/all-MiniLM-L6-v2",
                model_kwargs={'device': 'cpu'},
                encode_kwargs={'normalize_embeddings': True},
            )
            _emb_load_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(f"[CACHE] HuggingFaceEmbeddings ready in {_emb_load_ms} ms")
    return _emb_instance


def cleanup_old_audio_files(audio_folder: Path, max_age_seconds: int = 600):
    """Delete audio temp files older than max_age_seconds."""
    if not audio_folder.exists():
        return
    now = time.time()
    removed = errors = 0
    for f in audio_folder.iterdir():
        try:
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink()
                removed += 1
        except Exception as e:
            errors += 1
            logger.debug(f"[CLEANUP] Could not remove {f.name}: {e}")
    if removed:
        logger.info(f"[CLEANUP] Removed {removed} old audio files (errors={errors})")


def start_audio_cleanup_loop(audio_folder: Path):
    """Spawn a background greenlet that cleans audio files every 5 minutes."""
    import gevent

    def _loop():
        while True:
            gevent.sleep(300)
            try:
                cleanup_old_audio_files(audio_folder)
            except Exception as e:
                logger.error(f"[CLEANUP] Loop error: {e}")

    gevent.spawn(_loop)
    logger.info("[CLEANUP] Audio cleanup greenlet started (interval=5min, max_age=10min)")


def warmup_all():
    """
    Load local models at startup so the first request hits warm caches.

    Note: Groq API (STT) needs no local warm-up — it's a remote HTTP service.
          Only the embedding model is loaded locally now.
    """
    t0 = time.perf_counter()
    logger.info("[WARMUP] Starting background model warm-up…")
    errors = []

    try:
        get_embeddings()
    except Exception as e:
        logger.error(f"[WARMUP] Embeddings failed: {e}")
        errors.append(f"embeddings: {e}")

    total_ms = int((time.perf_counter() - t0) * 1000)
    if errors:
        logger.warning(f"[WARMUP] Completed with errors in {total_ms} ms: {errors}")
    else:
        logger.info(f"[WARMUP] All models warm in {total_ms} ms")
