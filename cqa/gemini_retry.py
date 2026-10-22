"""Shared retry policy for every Gemini call (upload, file processing, generate).

- 429 / RESOURCE_EXHAUSTED: wait the API's retryDelay (+2 s), growing per attempt, capped.
- 500 / 502 / 503 / 504 (google.genai errors.ServerError, "UNAVAILABLE", "overloaded"): exponential backoff + jitter.
- Anything else (e.g. 400 INVALID_ARGUMENT): not retried.
Every retry is logged server side; an optional callback lets the UI show "Gemini is busy, retrying…".
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, TypeVar

log = logging.getLogger("cqa.gemini")
T = TypeVar("T")

MAX_RETRIES = 4
SERVER_BASE_S = 2.0
SERVER_CAP_S = 60.0
RATE_CAP_S = 120.0
SERVER_CODES = {500, 502, 503, 504}
OnRetry = Callable[[int, int, str], None]  # (attempt, max_retries, kind)


def classify(exc: Exception) -> str | None:
    """'rate_limit', 'server', or None (do not retry)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    if code == 429 or "RESOURCE_EXHAUSTED" in text:
        return "rate_limit"
    if code in SERVER_CODES or type(exc).__name__ == "ServerError" or "UNAVAILABLE" in text or "overloaded" in text.lower():
        return "server"
    return None


def retry_delay(exc: Exception, attempt: int, rng: Callable[[], float] = random.random) -> float | None:
    kind = classify(exc)
    if kind == "rate_limit":
        text = str(exc)
        m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", text) or re.search(r"retry in (\d+(?:\.\d+)?)s", text)
        base = float(m.group(1)) if m else 30.0
        return min(base + 2.0, RATE_CAP_S) * (1 + attempt * 0.5)
    if kind == "server":
        return min(SERVER_BASE_S * (2 ** attempt), SERVER_CAP_S) + rng()
    return None


def retry_call(
    fn: Callable[[], T],
    what: str,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: OnRetry | None = None,
    rng: Callable[[], float] = random.random,
) -> T:
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            delay = retry_delay(exc, attempt, rng)
            if delay is None or attempt == max_retries:
                if delay is not None:
                    log.error("Gemini %s failed after %d retries: %r", what, max_retries, exc)
                raise
            kind = classify(exc) or "error"
            log.warning("Gemini %s %s (%s); retry %d/%d in %.1f s", what, kind, type(exc).__name__, attempt + 1, max_retries, delay)
            if on_retry is not None:
                on_retry(attempt + 1, max_retries, kind)
            sleep(delay)
    raise RuntimeError("unreachable")
