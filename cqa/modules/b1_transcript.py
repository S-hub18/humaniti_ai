"""B1 transcript-only scorer (spec §6).

Transcript source: an uploaded Zoom .vtt, else Gemini Flash transcribes the AUDIO only (extracted locally,
10-min chunks with 30 s overlap, uploaded one at a time and always deleted). Children are pseudonymised
locally (Kid1..N, Teacher, Video) and a fail-closed leak check runs before any text is sent for scoring.
A Gemini text call then scores the 6 non-visual rubric items; quotes that are not in the transcript are dropped.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from cqa.aggregate import ModuleVote
from cqa.evidence import fmt_ts, parse_ts, validate
from cqa.gemini_observer import GeminiObserver
from cqa.modules import ModuleResult
from cqa.rubric import ITEM_BY_ID, Status
from cqa.schema import ItemObservation, ObserverOutput, TranscriptTurn
from cqa.transcript import parse_vtt

B1_ITEMS = ("individual_calling", "rapport_intro", "energy_warmth", "homework", "closing", "new_joiner_intro")
B1_WEIGHT = 1.0  # one vote against the core's k=3 runs (spec §6)
B1_PROMPT_VERSION = "b1-v1"
CHUNK_S, OVERLAP_S = 600.0, 30.0
NAME_STOP = {
    "audio", "shared", "iphone", "ipad", "user", "galaxy", "phone", "laptop", "android", "samsung", "redmi",
    "teacher", "kid", "kids", "video", "media", "other", "mom", "mum", "dad", "sir", "maam", "madam", "the",
    "and", "zoom", "meeting", "guest", "unknown", "student", "parent", "speaker",
}
ROLE_ENUM = ["teacher", "kid", "media", "other"]


class LeakError(RuntimeError):
    """A known name survived pseudonymisation: nothing may be sent."""


# ---------------------------------------------------------------- pseudonymisation
def _name_tokens(label: str | None) -> set[str]:
    if not label:
        return set()
    toks = re.split(r"[^A-Za-z]+", label)
    return {t.lower() for t in toks if len(t) >= 3 and t.lower() not in NAME_STOP}


def pseudonymise(turns: list[TranscriptTurn], extra_names: tuple[str, ...] = ()) -> tuple[list[TranscriptTurn], dict]:
    """Replace speaker labels and spoken names with stable pseudonyms. Returns (turns, {"kids": n, "names": set})."""
    kid_ids: dict[str, str] = {}
    token_map: dict[str, str] = {}
    for t in turns:
        if t.role == "media":
            for tok in _name_tokens((t.speaker or "").replace("Audio shared by", "")):
                token_map.setdefault(tok, "Teacher")
        elif t.role == "teacher":
            for tok in _name_tokens(t.speaker):
                token_map.setdefault(tok, "Teacher")
        else:
            key = (t.speaker or "").strip().lower() or "?"
            if key not in kid_ids:
                kid_ids[key] = f"Kid{len(kid_ids) + 1}"
            for tok in _name_tokens(t.speaker):
                token_map.setdefault(tok, kid_ids[key])
    for n in extra_names:
        for tok in _name_tokens(n):
            token_map.setdefault(tok, "Kid")
    rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, token_map), key=len, reverse=True)) + r")\b", re.I) if token_map else None

    def sub(text: str) -> str:
        return rx.sub(lambda m: token_map[m.group(1).lower()], text) if rx else text

    out = []
    for t in turns:
        if t.role == "media":
            spk = "Video"
        elif t.role == "teacher":
            spk = "Teacher"
        else:
            spk = kid_ids[(t.speaker or "").strip().lower() or "?"]
        out.append(t.model_copy(update={"text": sub(t.text), "speaker": spk}))
    return out, {"kids": len(kid_ids), "names": set(token_map)}


def leak_check(turns: list[TranscriptTurn], names: set[str]) -> None:
    if not names:
        return
    rx = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b", re.I)
    for t in turns:
        if rx.search(t.text) or rx.search(t.speaker or ""):
            raise LeakError("a known name survived pseudonymisation")


# ---------------------------------------------------------------- chunked transcription
def plan_chunks(duration: float, chunk_s: float = CHUNK_S, overlap_s: float = OVERLAP_S) -> list[tuple[float, float]]:
    out, start, step = [], 0.0, chunk_s - overlap_s
    while start < duration:
        end = min(start + chunk_s, duration)
        out.append((start, end))
        if end >= duration:
            break
        start += step
    return out


def stitch(chunks: list[tuple[float, float]], per_chunk: list[list[TranscriptTurn]]) -> list[TranscriptTurn]:
    """Chunk-relative -> absolute times; each overlap is split at its midpoint so no turn appears twice."""
    out = []
    for i, ((s, _e), turns) in enumerate(zip(chunks, per_chunk)):
        lo = -1.0 if i == 0 else (s + (chunks[i - 1][1] - s) / 2)
        hi = float("inf") if i == len(chunks) - 1 else (chunks[i + 1][0] + (chunks[i][1] - chunks[i + 1][0]) / 2)
        for t in turns:
            a = s + t.start
            if lo <= a < hi:
                out.append(t.model_copy(update={"start": a, "end": s + t.end}))
    return sorted(out, key=lambda t: t.start)


_TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"}, "end": {"type": "string"},
                    "role": {"type": "string", "enum": ROLE_ENUM},
                    "speaker_hint": {"type": "string"}, "text": {"type": "string"},
                },
                "required": ["start", "role", "text"],
            },
        }
    },
    "required": ["turns"],
}
_TRANSCRIBE_PROMPT = (
    "Transcribe this audio clip from an online chess class for children. Return JSON turns with start/end as MM:SS "
    "from the start of THIS clip, role (teacher = the adult leading the class; kid = a child; media = a played lesson "
    "video or other recorded narration; other), a short speaker_hint (a first name if one is clearly used, else "
    "'teacher' or 'kid'), and the verbatim text. Keep the original language."
)


def _call(client, model: str, contents, config, sleep, max_retries: int = 4):
    for attempt in range(max_retries + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            delay = GeminiObserver._retry_delay(exc)
            if delay is None or attempt == max_retries:
                raise
            sleep(min(delay + 2.0, 120.0) * (1 + attempt * 0.5))


def _config(schema: dict):
    from google.genai import types

    return types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json", response_json_schema=schema)


def _wait_active(client, f, sleep, max_wait_s: float = 600.0):
    waited = 0.0
    while f.state.name == "PROCESSING":
        if waited > max_wait_s:
            raise RuntimeError("Gemini audio processing timed out")
        sleep(2.0)
        waited += 2.0
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini audio processing failed: {f.state.name}")
    return f


def transcribe_audio(media_path, client, model: str, duration: float, chunk_s: float = CHUNK_S,
                     overlap_s: float = OVERLAP_S, sleep=time.sleep, min_interval_s: float = 0.0) -> list[TranscriptTurn]:
    from google.genai import types

    chunks = plan_chunks(duration, chunk_s, overlap_s)
    tmp = Path(tempfile.mkdtemp(prefix="cqa_b1_"))
    per_chunk: list[list[TranscriptTurn]] = []
    last = None
    try:
        for i, (s, e) in enumerate(chunks):
            local = tmp / f"chunk{i:03d}.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s}", "-t", f"{e - s}", "-i", str(media_path), "-vn",
                            "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", str(local)], check=True)
            remote = client.files.upload(file=str(local), config={"mime_type": "audio/mp4"})
            local.unlink(missing_ok=True)
            try:
                remote = _wait_active(client, remote, sleep)
                if last is not None and min_interval_s > 0:
                    gap = min_interval_s - (time.monotonic() - last)
                    if gap > 0:
                        sleep(gap)
                part = types.Part(file_data=types.FileData(file_uri=remote.uri, mime_type=remote.mime_type))
                turns = []
                for _attempt in range(2):
                    resp = _call(client, model, [part, _TRANSCRIBE_PROMPT], _config(_TRANSCRIPT_SCHEMA), sleep)
                    last = time.monotonic()
                    try:
                        data = json.loads(resp.text)
                        for t in data["turns"]:
                            st = parse_ts(t["start"])
                            en = parse_ts(t.get("end") or t["start"])
                            turns.append(TranscriptTurn(start=st, end=max(en, st), role=t["role"], text=t["text"],
                                                        speaker=t.get("speaker_hint") or None))
                        break
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        turns = []
                per_chunk.append(turns)
            finally:
                client.files.delete(name=remote.name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return stitch(chunks, per_chunk)


# ---------------------------------------------------------------- judging
def _items_schema() -> dict:
    ev = {"type": "object", "properties": {"t_start": {"type": "string"}, "quote": {"type": "string"},
                                           "observation": {"type": "string"}}, "required": ["t_start", "quote", "observation"]}
    item = {"type": "object", "properties": {"status": {"type": "string", "enum": [s.value for s in Status]},
                                             "evidence": {"type": "array", "items": ev}, "rationale": {"type": "string"}},
            "required": ["status", "evidence", "rationale"]}
    return {"type": "object", "properties": {"items": {"type": "object", "properties": {i: item for i in B1_ITEMS},
                                                       "required": list(B1_ITEMS)}}, "required": ["items"]}


def _judge_prompt(turns: list[TranscriptTurn]) -> str:
    rubric = []
    for i in B1_ITEMS:
        d = ITEM_BY_ID[i]
        rubric.append(f"- {i} ({d.title}): {d.description}\n" + "\n".join(f"    - {k}: {v}" for k, v in d.anchors.items()))
    lines = "\n".join(f"[{fmt_ts(t.start)}] {t.speaker}: {t.text}" for t in turns)
    return (
        "You review the TRANSCRIPT of an online chess class for children (8x8 academy). Children are pseudonymised as "
        "Kid1..KidN; 'Video' lines are a played lesson video, not the teacher. Score each checklist item from the "
        "transcript only. Every 'met' or 'partial' needs evidence: t_start as MM:SS copied from the transcript line, and a "
        "VERBATIM quote copied from that line. Use 'not_observable' if the transcript cannot show it; use "
        "'not_applicable' for new_joiner_intro only when no new child joins. You observe; you do not grade.\n\n"
        "Checklist:\n" + "\n".join(rubric) + "\n\nTranscript:\n" + lines
    )


def judge(turns: list[TranscriptTurn], client, model: str, duration: float, sleep=time.sleep) -> ModuleResult:
    resp = None
    for _attempt in range(2):
        resp = _call(client, model, [_judge_prompt(turns)], _config(_items_schema()), sleep)
        try:
            data = json.loads(resp.text)["items"]
            items = {i: ItemObservation.model_validate(data[i]) for i in B1_ITEMS if i in data}
            break
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            items = None
    if items is None:
        return ModuleResult(module="b1", ok=False, error="unparseable judge output")
    obs = ObserverOutput(call="main", window=(0.0, duration), items=items)
    obs, flags = validate(obs, duration, transcript=turns)
    votes = [ModuleVote(module="b1", item=i, status=o.status, weight=B1_WEIGHT, evidence=o.evidence)
             for i, o in obs.items.items() if o.status in (Status.MET, Status.PARTIAL, Status.NOT_MET)]
    u = getattr(resp, "usage_metadata", None)
    return ModuleResult(module="b1", votes=votes, flags=flags, features={
        "statuses": {i: o.status.value for i, o in obs.items.items()},
        "input_tokens": int(getattr(u, "prompt_token_count", 0) or 0),
    })


def run_b1(media_path, vtt_path, client, model: str, duration: float, sleep=time.sleep,
           min_interval_s: float = 0.0) -> ModuleResult:
    if vtt_path is not None:
        turns, source = parse_vtt(Path(vtt_path).read_text(encoding="utf-8", errors="replace")), "vtt"
    else:
        turns = transcribe_audio(media_path, client, model, duration, sleep=sleep, min_interval_s=min_interval_s)
        source = "gemini_audio"
    pseudo, mapping = pseudonymise(turns)
    try:
        leak_check(pseudo, mapping["names"])
    except LeakError as exc:
        return ModuleResult(module="b1", ok=False, error=f"leak check failed ({exc}); nothing was sent for scoring",
                            features={"source": source, "leak_check": "failed"})
    res = judge(pseudo, client, model, duration, sleep=sleep)
    res.features.update(source=source, n_turns=len(pseudo), n_kids=mapping["kids"], leak_check="passed")
    return res
