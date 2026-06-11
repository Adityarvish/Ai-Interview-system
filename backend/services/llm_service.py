import logging
import os
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import Config

logger = logging.getLogger(__name__)


GROQ_API_URL     = "https://api.groq.com/openai/v1/chat/completions"
PRIMARY_MODEL    = os.environ.get("GROQ_PRIMARY_MODEL",  "llama-3.3-70b-versatile")
FALLBACK_MODEL   = os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
DEFAULT_TIMEOUT  = int(os.environ.get("GROQ_REQUEST_TIMEOUT", "120"))


import threading as _threading
_session_lock     = _threading.Lock()
_session_instance: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session_instance
    if _session_instance is not None:
        return _session_instance
    with _session_lock:
        if _session_instance is None:
            s = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=8,
                pool_maxsize=8,
                max_retries=Retry(
                    total=2,
                    backoff_factor=0.5,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=["POST"],
                ),
            )
            s.mount("https://", adapter)
            s.mount("http://",  adapter)
            _session_instance = s
            logger.info("[LLM] Groq requests.Session created (pool_size=8)")
    return _session_instance


def _groq_post_in_thread(
    api_key: str,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> str:
    """
    Blocking POST to Groq API.
    NEVER call directly from a greenlet — use GroqService._call_in_threadpool().
    Returns the assistant content string on success.
    Raises requests.RequestException or ValueError on failure.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }

    response = _get_session().post(
        GROQ_API_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
    )

    if response.status_code != 200:
        try:
            detail = response.json().get("error", {}).get("message", response.text[:200])
        except Exception:
            detail = response.text[:200]
        raise requests.HTTPError(
            f"Groq API {response.status_code}: {detail}",
            response=response,
        )

    data    = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("Groq API returned no choices")

    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip()
    if not content:
        raise ValueError("Groq API returned an empty response")

    return content


def _run_in_threadpool(fn, *args, **kwargs):
    """
    Execute a blocking callable in the gevent threadpool.
    Falls back to a direct call if gevent is unavailable or the caller is
    already in an OS thread (not a hub greenlet).
    """
    try:
        from gevent import get_hub
        import greenlet as _gl

        hub     = get_hub()
        current = _gl.getcurrent()

        if current is hub.greenlet:
            return fn(*args, **kwargs)

        return hub.threadpool.apply(fn, args, kwargs)

    except ImportError:
        return fn(*args, **kwargs)
    except Exception:
        return fn(*args, **kwargs)


class GroqService:
    """
    Drop-in replacement for OllamaService.
    Public methods: generate(), generate_short(), check_connection()
    """

    def __init__(self):
        self.api_key        = Config.GROQ_API_KEY
        self.primary_model  = PRIMARY_MODEL
        self.fallback_model = FALLBACK_MODEL

        if not self.api_key:
            logger.error(
                "[LLM] GROQ_API_KEY is not set. "
                "Add it to backend/.env and restart."
            )


    def _call(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> str:
        """Single model call via gevent-safe threadpool dispatch."""
        messages = [{"role": "user", "content": prompt}]
        return _run_in_threadpool(
            _groq_post_in_thread,
            self.api_key,
            model,
            messages,
            temperature,
            max_tokens,
            timeout,
        )


    def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int    = 512,
        timeout: int       = DEFAULT_TIMEOUT,
    ) -> str:
        """
        Generate a completion for *prompt*.
        Tries the primary model first; falls back to the fallback model on any
        failure (timeout, HTTP error, empty response).
        Raises the fallback's exception if both models fail.
        """
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY not configured")

        t0 = time.perf_counter()
        logger.debug(
            f"[LLM] generate prompt_len={len(prompt)} "
            f"max_tokens={max_tokens} model={self.primary_model}"
        )

        try:
            text    = self._call(prompt, self.primary_model, temperature, max_tokens, timeout)
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.info(
                f"[LLM] generate OK model={self.primary_model} "
                f"→ {len(text)} chars in {elapsed} ms"
            )
            return text

        except Exception as primary_exc:
            logger.warning(
                f"[LLM] Primary model '{self.primary_model}' failed "
                f"({type(primary_exc).__name__}: {primary_exc}). "
                f"Activating fallback '{self.fallback_model}'…"
            )

        try:
            text    = self._call(prompt, self.fallback_model, temperature, max_tokens, timeout)
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.info(
                f"[LLM] generate OK (fallback) model={self.fallback_model} "
                f"→ {len(text)} chars in {elapsed} ms"
            )
            return text

        except Exception as fallback_exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.error(
                f"[LLM] Both models failed after {elapsed} ms. "
                f"Fallback error: {fallback_exc}"
            )
            raise

    def generate_short(
        self,
        prompt: str,
        temperature: float = 0.1,
        max_tokens: int    = 120,
    ) -> str:
        """Fast path for short completions (tech-stack extraction, etc.)."""
        return self.generate(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=30,
        )

    def check_connection(self) -> bool:
        """
        Verify Groq API reachability with a minimal probe call.
        Returns True if the API responds; False on any error.
        """
        if not self.api_key:
            return False
        try:
            self._call("Say OK", self.primary_model, 0.0, 5, 15)
            return True
        except Exception as exc:
            logger.warning(f"[LLM] check_connection failed: {exc}")
            return False


OllamaService = GroqService
