"""Timestamp parsing and evidence validation (spec §4.4).

The LLM's evidence is untrusted: anything that cannot be located in the recording
(or matched to the transcript) is dropped before scoring.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from cqa.rubric import Status
from cqa.schema import ObserverOutput, TranscriptTurn

WINDOW_TOLERANCE_S = 5.0
QUOTE_RADIUS_S = 20.0
QUOTE_MIN_RATIO = 60.0

_TS = re.compile(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?\s*$")


def parse_ts(s: str) -> float:
    """Parse 'MM:SS', 'M:SS', 'H:MM:SS' or overflowing 'MM:SS' (e.g. '75:30') into seconds."""
    m = _TS.match(s or "")
    if not m:
        raise ValueError(f"bad timestamp: {s!r}")
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:  # MM:SS (minutes may exceed 59)
        mins, secs = int(a), float(b)
        if secs >= 60:
            raise ValueError(f"bad timestamp: {s!r}")
        return mins * 60 + secs
    hours, mins, secs = int(a), int(b), float(c)
    if mins >= 60 or secs >= 60:
        raise ValueError(f"bad timestamp: {s!r}")
    return hours * 3600 + mins * 60 + secs


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _quote_matches(quote: str, t: float, transcript: list[TranscriptTurn]) -> bool:
    for turn in transcript:
        if turn.end < t - QUOTE_RADIUS_S or turn.start > t + QUOTE_RADIUS_S:
            continue
        if fuzz.token_set_ratio(quote, turn.text) >= QUOTE_MIN_RATIO:
            return True
    return False


def validate(
    obs: ObserverOutput,
    duration: float,
    transcript: list[TranscriptTurn] | None = None,
) -> tuple[ObserverOutput, list[str]]:
    """Drop evidence that cannot be located; demote unsupported met/partial claims."""
    lo = max(0.0, obs.window[0] - WINDOW_TOLERANCE_S)
    hi = min(duration, obs.window[1] + WINDOW_TOLERANCE_S)
    flags: list[str] = []
    new_items = {}
    for item_id, item in obs.items.items():
        kept = []
        for ev in item.evidence:
            try:
                t = parse_ts(ev.t_start)
            except ValueError:
                continue
            if not (0.0 <= t <= duration and lo <= t <= hi):
                continue
            if ev.quote and transcript is not None and not _quote_matches(ev.quote, t, transcript):
                continue
            kept.append(ev)
        status = item.status
        if status in (Status.MET, Status.PARTIAL) and not kept:
            status = Status.NOT_OBSERVABLE
            flags.append(f"unsupported_claim:{item_id}")
        new_items[item_id] = item.model_copy(update={"evidence": kept, "status": status})
    return obs.model_copy(update={"items": new_items}), flags
