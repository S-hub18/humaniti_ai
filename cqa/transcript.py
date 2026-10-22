"""Zoom .vtt -> TranscriptTurn list (roles: teacher / kid / media). Never logs transcript text."""

from __future__ import annotations

import re
from collections import Counter

from cqa.schema import TranscriptTurn

_CUE = re.compile(r"(\d+:\d{2}:\d{2}\.\d+) --> (\d+:\d{2}:\d{2}\.\d+)[^\n]*\n(.+?)(?:\n\s*\n|\Z)", re.S)
_SPK = re.compile(r"^([^:]{1,60}):\s+(.*)$", re.S)


def _sec(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(text: str) -> list[TranscriptTurn]:
    raw = []
    for a, b, body in _CUE.findall(text.replace("\r\n", "\n")):
        body = " ".join(body.strip().splitlines())
        m = _SPK.match(body)
        spk, utt = (m.group(1).strip(), m.group(2).strip()) if m else ("", body)
        raw.append((_sec(a), _sec(b), spk, utt))
    talk: Counter = Counter()
    for a, b, spk, _ in raw:
        if spk and not spk.lower().startswith("audio shared by"):
            talk[spk] += b - a
    teacher = talk.most_common(1)[0][0] if talk else None
    turns = []
    for a, b, spk, utt in raw:
        if spk.lower().startswith("audio shared by"):
            role = "media"
        elif spk and spk == teacher:
            role = "teacher"
        elif spk:
            role = "kid"
        else:
            role = "other"
        turns.append(TranscriptTurn(start=a, end=b, role=role, text=utt))
    return turns
