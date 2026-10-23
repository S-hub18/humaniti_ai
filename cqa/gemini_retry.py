"""Shared retry policy for every Gemini call (upload, file processing, generate).

- 429 / RESOURCE_EXHAUSTED: wait the API's retryDelay (+2 s), growing per attempt, capped.
- 500 / 502 / 503 / 504 (google.genai errors.ServerError, "UNAVAILABLE", "overloaded"): exponential backoff + jitter.
- Anything else (e.g. 400 INVALID_ARGUMENT): not retried.
Every retry is logged server side; an optional callback lets the UI show "Gemini is busy, retrying…".
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from typing import Callable, TypeVar

log = logging.getLogger("cqa.gemini")
T = TypeVar("T")

MAX_RETRIES = 4
SERVER_BASE_S = 2.0
SERVER_CAP_S = 60.0
RATE_CAP_S = 90.0
BACKOFF_CAP_S = 90.0  # no single retry wait is longer than this
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
        return min(min(base + 2.0, RATE_CAP_S) * (1 + attempt * 0.5), BACKOFF_CAP_S)
    if kind == "server":
        return min(min(SERVER_BASE_S * (2 ** attempt), SERVER_CAP_S) + rng(), BACKOFF_CAP_S)
    return None


OnWait = Callable[[int, int, int, str], None]  # (seconds_remaining, attempt, max_retries, kind)


def countdown_sleep(seconds: float, on_tick: Callable[[int], None], sleep: Callable[[float], None] = time.sleep) -> None:
    """Sleep `seconds`, calling on_tick(whole seconds remaining) once per second, so the UI can show a live countdown."""
    rem = float(seconds)
    while rem > 1e-9:
        on_tick(math.ceil(rem - 1e-9))
        step = rem - (math.ceil(rem - 1e-9) - 1)
        sleep(step)
        rem -= step


def retry_call(
    fn: Callable[[], T],
    what: str,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: OnRetry | None = None,
    rng: Callable[[], float] = random.random,
    on_wait: OnWait | None = None,
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
            if kind == "rate_limit":
                log.warning("Gemini 429 full error (quota metric, retryDelay): %s", str(exc))
            if on_retry is not None:
                on_retry(attempt + 1, max_retries, kind)
            if on_wait is not None:
                countdown_sleep(delay, lambda rem: on_wait(rem, attempt + 1, max_retries, kind), sleep)
            else:
                sleep(delay)
    raise RuntimeError("unreachable")



# ------------------------------------------------------------------ proactive pacing
class ReviewCancelled(RuntimeError):
    """The user's session ended or reran: stop before the next Gemini call (uploads are deleted by the caller)."""


def tpm_budget() -> int:
    """GEMINI_TPM env (input tokens per minute). Default 200k on the free tier (limit ~250k), generous on paid."""
    import os

    from cqa.config import free_tier

    raw = os.environ.get("GEMINI_TPM")
    if raw:
        return int(raw)
    return 200_000 if free_tier() else 4_000_000


def estimate_video_tokens(seconds: float, media_resolution: str) -> int:
    per_s = 100 if media_resolution == "low" else 290  # ~66 or ~258 tok/frame at 1 fps + 32 tok/s audio
    return int(seconds * per_s)


def estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class TokenBucket:
    """Sliding 60 s window of input tokens sent; acquire() waits until a call fits under the TPM budget.
    Shared per process, so every session on this server paces against the same key quota."""

    def __init__(self, tpm: int | None = None, window_s: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        import threading

        self._tpm = tpm
        self.window_s = window_s
        self.clock = clock
        self.sleep = sleep
        self.events: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    @property
    def tpm(self) -> int:
        return self._tpm if self._tpm is not None else tpm_budget()

    def _used(self, now: float) -> int:
        self.events = [(t, n) for t, n in self.events if now - t < self.window_s]
        return sum(n for _, n in self.events)

    def wait_needed(self, tokens: int) -> float:
        with self._lock:
            now = self.clock()
            used = self._used(now)
            if not self.events or used + tokens <= self.tpm:
                return 0.0
            freed, need = 0, used + tokens - self.tpm
            for t, n in self.events:
                freed += n
                if freed >= need:
                    return max(0.0, t + self.window_s - now)
            return max(0.0, self.events[-1][0] + self.window_s - now)

    def acquire(self, tokens: int, on_wait: Callable[[int], None] | None = None) -> None:
        while True:
            wait = self.wait_needed(tokens)
            if wait <= 0:
                with self._lock:
                    self.events.append((self.clock(), tokens))
                return
            if on_wait is not None:
                countdown_sleep(wait, on_wait, self.sleep)
            else:
                self.sleep(wait)


BUCKET = TokenBucket()
