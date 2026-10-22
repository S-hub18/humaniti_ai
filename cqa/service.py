"""Service layer used by the app: fetch input, run the assessment end to end, build a JSON report.

Privacy: every temporary file this module creates (downloaded video, extracted audio, sampled
frames) lives in a temp dir that is deleted in `finally`. Transcript text is never logged.
"""

from __future__ import annotations

import ipaddress
import logging
from contextlib import contextmanager
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from cqa.config import call_spacing_s, load_dotenv, model_id
from cqa.evidence import parse_ts
from cqa.explain import ABSENCE, build_explanation
from cqa.media import probe
from cqa.modules import ModuleResult
from cqa.observer import MockObserver
from cqa.pipeline import PipelineConfig, assess
from cqa.rubric import ITEMS, STATUS_VALUE, Status
from cqa.schema import TranscriptTurn
from cqa.transcript import parse_vtt

log = logging.getLogger("cqa.service")
Progress = Callable[[str, float], None]
DOWNLOAD_DEADLINE_S = 20 * 60
MAX_BYTES = 600 * 1024 * 1024  # matches .streamlit maxUploadSize
DRIVE_HOSTS = ("drive.google.com", "docs.google.com")

# Synthetic demo class for MOCK mode (no API calls). Clearly labelled in the report.
MOCK_ORACLE = {
    "individual_calling": Status.MET,
    "rapport_intro": Status.MET,
    "energy_warmth": Status.PARTIAL,
    "camera_background": Status.MET,
    "homework": Status.NOT_MET,
    "closing": Status.PARTIAL,
    "new_joiner_intro": Status.NOT_APPLICABLE,
}


class InputError(ValueError):
    """Bad user input (unsupported link, too large, unreadable file)."""


LOST_UPLOAD = "Your upload was lost (the app restarted). Remove the file and upload it again."
NOT_A_VIDEO = "Could not read this file as a video. Upload an mp4, mov, mkv or webm recording."
_FFPROBE_INVALID = ("Invalid data found when processing input", "moov atom not found", "could not find codec",
                    "End of file")


def check_upload_bytes(data: bytes | memoryview, expected_size: int | None) -> None:
    """An empty or truncated upload buffer (e.g. the server restarted while the browser still showed the file)."""
    n = len(data)
    if n == 0 or (expected_size is not None and expected_size > 0 and n != expected_size):
        log.warning("upload buffer has %d bytes, expected %s", n, expected_size)
        raise InputError(LOST_UPLOAD)


def _probe_or_explain(path: Path):
    """probe() with honest errors: lost upload vs not a video vs other ffprobe failures (always logged)."""
    import subprocess as _sp

    if not Path(path).exists() or Path(path).stat().st_size == 0:
        raise InputError(LOST_UPLOAD)
    try:
        return probe(path)
    except _sp.CalledProcessError as exc:
        err = (exc.stderr or "").strip()
        log.error("ffprobe failed (exit %s) on upload: %s", exc.returncode, err[-500:])
        if any(sig.lower() in err.lower() for sig in _FFPROBE_INVALID):
            raise InputError(NOT_A_VIDEO) from exc
        raise InputError("The recording could not be read (ffprobe failed). Try uploading it again.") from exc
    except (ValueError, KeyError) as exc:  # ffprobe ran but returned no duration
        log.error("ffprobe returned no usable duration: %r", exc)
        raise InputError(NOT_A_VIDEO) from exc


def _gdown(url: str, out: Path) -> Path:
    import gdown

    res = gdown.download(url, str(out), quiet=True, fuzzy=True)
    if not res:
        raise InputError("Google Drive download failed. Check that the link is shared as 'Anyone with the link'.")
    return Path(res)


def check_public_url(url: str) -> None:
    """Reject non-http(s) URLs and hosts that resolve to private, loopback, link-local or reserved IPs (SSRF)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise InputError("Paste an http(s) link: a Google Drive share link or a direct link to a video file.")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise InputError(f"Could not resolve {u.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise InputError("That link points to a private or local address, which is not allowed.")


def _http(url: str, out: Path, max_bytes: int) -> Path:
    import requests

    for _ in range(5):  # follow redirects manually so every hop is checked
        check_public_url(url)
        r = requests.get(url, stream=True, timeout=60, allow_redirects=False)
        if r.is_redirect and r.headers.get("location"):
            url = requests.compat.urljoin(url, r.headers["location"])
            r.close()
            continue
        break
    else:
        raise InputError("Too many redirects.")
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_S
    try:
        with r:
            r.raise_for_status()
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise InputError(f"File is larger than {max_bytes // 1_000_000} MB.")
            total = 0
            with out.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    total += len(chunk)
                    if total > max_bytes:
                        raise InputError(f"File is larger than {max_bytes // 1_000_000} MB.")
                    if time.monotonic() > deadline:
                        raise InputError("The download took too long. Upload the file directly instead.")
                    fh.write(chunk)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return out


def fetch_input(
    source: str,
    dest_dir: Path,
    max_bytes: int = MAX_BYTES,
    drive_fn: Callable[[str, Path], Path] = _gdown,
    http_fn: Callable[[str, Path, int], Path] | None = None,
) -> Path:
    """Download a Google Drive share link or a direct http(s) link into dest_dir."""
    u = urlparse(source.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise InputError("Paste an http(s) link: a Google Drive share link or a direct link to a video file.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "input_video"
    if u.hostname in DRIVE_HOSTS:
        path = drive_fn(source, out)
    else:
        path = (http_fn or _http)(source, out, max_bytes)
    if path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise InputError(f"File is larger than {max_bytes // 1_000_000} MB.")
    return path


def _observer(mode: str, tier: str, on_retry=None):
    if mode == "mock":
        return MockObserver(oracle=MOCK_ORACLE, noise=0.1, seed=7), "mock"
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Gemini mode needs GEMINI_API_KEY. Set it in the environment, or use Mock mode.")
    from google import genai

    from cqa.gemini_observer import GeminiObserver

    # No CachedObserver here: the app must not persist observations about a class (cache is eval-only).
    model = model_id(tier)
    return GeminiObserver(_gemini_client(), model=model, on_retry=on_retry, min_call_interval_s=call_spacing_s()), model


STEPS = {"main": ("main video call", 0.6), "opening": ("opening 10 minutes", 0.75), "closing": ("closing 10 minutes", 0.88)}


def _eta_s(name: str, duration: float) -> float:
    """Rough per-call time from what we measured (B472: ~390 s for the 56-min main call incl. upload)."""
    if name == "main":
        return 60.0 + 330.0 * duration / 3600.0
    return 90.0 + call_spacing_s()


def _attach_progress(observer, duration: float, say: Progress) -> None:
    """Hook live step + ETA and countdown messages into a Gemini observer (no-op for observers without hooks)."""
    if hasattr(observer, "on_call"):
        def on_call(name: str) -> None:
            label, frac = STEPS.get(name, (name, 0.6))
            say(f"Step: {label} (about {max(1, round(_eta_s(name, duration) / 60))} min)", frac)
        observer.on_call = on_call
    if hasattr(observer, "on_wait"):
        def on_wait(rem: int, attempt: int, max_retries: int, kind: str) -> None:
            if kind == "spacing":
                say(f"Free-tier pacing: next Gemini call in {rem}s", 0.6)
            elif kind == "rate_limit":
                say(f"Rate limit hit, waiting {rem}s before retrying (attempt {attempt}/{max_retries})", 0.6)
            else:
                say(f"Gemini is busy, waiting {rem}s before retrying (attempt {attempt}/{max_retries})", 0.6)
        observer.on_wait = on_wait


class ReviewGate:
    """One Gemini review at a time across all app sessions (shared quota). Others wait and are told they're next."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()

    @contextmanager
    def slot(self, on_wait: Callable[[float], None], poll_s: float = 2.0):
        waited = 0.0
        while not self._lock.acquire(timeout=poll_s):
            waited += poll_s
            on_wait(waited)
        try:
            yield
        finally:
            self._lock.release()


GATE = ReviewGate()


def _gemini_client():
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Gemini mode needs GEMINI_API_KEY. Set it in the environment, or use Mock mode.")
    from google import genai

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": 600_000})  # ms; no hung calls


def _run_b1(media, vtt_path, mode: str, observer) -> ModuleResult:
    """B1 needs Gemini for judging (and for transcription when no .vtt is given)."""
    if mode != "gemini":
        return ModuleResult(module="b1", ok=False, error="the transcript judge needs Gemini mode")
    from cqa.modules.b1_transcript import run_b1

    try:
        return run_b1(media.path, vtt_path, observer.client, observer.model, media.duration,
                      min_interval_s=observer.min_call_interval_s)
    except Exception as exc:
        return ModuleResult(module="b1", ok=False, error=type(exc).__name__)


def _secs(ts: str | None) -> float | None:
    try:
        return parse_ts(ts) if ts else None
    except ValueError:
        return None


def run_assessment(
    video_path: Path,
    vtt_path: Path | None = None,
    mode: Literal["mock", "gemini"] = "mock",
    tier: str = "flash",
    modules: dict[str, bool] | None = None,
    progress: Progress | None = None,
    b2_vote: bool = False,
) -> dict:
    """Run the full assessment and return a JSON-serialisable report."""
    modules = {"b2": True, "xc": True} if modules is None else modules
    say = progress or (lambda _msg, _frac: None)
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="cqa_run_"))
    try:
        say("Reading the recording", 0.05)
        media = _probe_or_explain(Path(video_path))
        transcript: list[TranscriptTurn] | None = None
        if vtt_path is not None:
            transcript = parse_vtt(Path(vtt_path).read_text(encoding="utf-8", errors="replace"))

        results = {}
        if modules.get("b2"):
            say("Measuring audio delivery (local)", 0.15)
            from cqa.modules.b2_audio import analyze_audio, extract_audio

            wav = tmp / "audio.wav"
            try:
                extract_audio(media.path, wav)
                teacher = [t for t in transcript if t.role == "teacher"] if transcript else None
                results["b2"] = analyze_audio(wav, teacher_turns=teacher)
            except Exception as exc:  # e.g. no audio track: the module fails, the review continues
                results["b2"] = ModuleResult(module="b2", ok=False, error=type(exc).__name__)
            finally:
                wav.unlink(missing_ok=True)
        if modules.get("xc"):
            say("Checking camera and background on sampled frames (local)", 0.35)
            from cqa.modules.xc_frames import crosscheck

            try:
                results["xc"] = crosscheck(media.path, n=60)
            except Exception as exc:
                results["xc"] = ModuleResult(module="xc", ok=False, error=type(exc).__name__)

        say("Observing the class against the rubric", 0.55)
        def busy(attempt: int, max_retries: int, kind: str) -> None:
            what = "busy" if kind == "server" else "rate-limiting us"
            say(f"Gemini is {what}, retrying… (attempt {attempt}/{max_retries})", 0.55)

        observer, model = _observer(mode, tier, on_retry=busy)
        _attach_progress(observer, media.duration, say)
        if modules.get("b1"):
            say("Scoring the transcript (B1)", 0.45)
            results["b1"] = _run_b1(media, vtt_path, mode, observer)
        votes = [v for k, r in results.items() for v in r.votes if k != "b2" or b2_vote]
        gates = [g for r in results.values() for g in r.gates]
        flags = [f for r in results.values() for f in r.flags]
        run = assess(media, observer, PipelineConfig(), transcript=transcript,
                     module_votes=votes, gates=gates, extra_flags=flags)
        say("Writing the report", 0.95)
        return _report(run, media.duration, mode, model, results, time.monotonic() - t0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _report(run, duration: float, mode: str, model: str, results: dict, wall: float) -> dict:
    a = run.assessment
    ex = build_explanation(a)
    items, markers = [], []
    for d in ITEMS:
        ic = a.items[d.id]
        evid = [e.model_dump() for e in ic.evidence]
        items.append({
            "id": d.id, "title": d.title, "weight": d.weight, "status": ic.status.value,
            "scored": STATUS_VALUE[ic.status] is not None, "agreement": round(ic.agreement, 2),
            "votes_total": len(ic.votes), "votes_agree": sum(1 for v in ic.votes if v == ic.status),
            "absence": _sentence(ABSENCE.get(d.id, "").removeprefix("Missing: ")) if ic.status == Status.NOT_MET else "",
            "description": d.description, "evidence": evid,
        })
        for e in ic.evidence:
            t = _secs(e.t_start)
            if t is not None:
                markers.append({"t": t, "label": e.t_start, "item": d.title, "status": ic.status.value})
    starts = [s for s in (_secs(r.class_start) for r in run.runs) if s is not None]
    ends = [s for s in (_secs(r.class_end) for r in run.runs) if s is not None]
    return {
        "mode": mode,
        "model": model,
        "grade": int(a.grade) if a.grade is not None else None,
        "label": a.grade.label if a.grade is not None else None,
        "score": a.score,
        "confidence": a.confidence,
        "session_type": a.session_type,
        "abstain_reason": a.abstain_reason,
        "coverage": a.coverage,
        "headline": ex["headline"],
        "strengths": ex["strengths"],
        "improvements": ex["improvements"],
        "flags": a.flags,
        "flag_text": ex["flags"],
        "camera": a.camera.model_dump(),
        "items": items,
        "timeline": {
            "duration": duration,
            "class_start": min(starts) if starts else None,
            "class_end": max(ends) if ends else None,
            "markers": sorted(markers, key=lambda m: m["t"]),
        },
        "modules": {k: {"ok": r.ok, "error": r.error, "features": r.features, "flags": r.flags} for k, r in results.items()},
        "cost_usd": round(sum(s.cost_usd for s in run.stats), 4),
        "calls": len(run.stats),
        "input_tokens": sum(s.input_tokens for s in run.stats),
        "output_tokens": sum(s.output_tokens for s in run.stats),
        "latency_s": round(wall, 1),
        "review_type": "video",
    }


# ------------------------------------------------------------------ transcript-only review
class _MockJudgeClient:
    """Mock-mode stand-in for the Gemini text judge: MOCK_ORACLE statuses, evidence quoted from real (pseudonymised)
    teacher lines so it survives validation. No API calls."""

    def __init__(self, turns: list[TranscriptTurn]):
        from cqa.evidence import fmt_ts

        teacher = [t for t in turns if t.role == "teacher"] or turns
        self._ev = [{"t_start": fmt_ts(t.start), "quote": t.text[:120], "observation": "mock evidence (demo observer)"}
                    for t in teacher[:3]]
        self.models = self

    def generate_content(self, model, contents, config):
        import json as _json
        from types import SimpleNamespace

        from cqa.modules.b1_transcript import B1_ITEMS

        items = {}
        for i in B1_ITEMS:
            st = MOCK_ORACLE.get(i, Status.NOT_OBSERVABLE)
            ev = self._ev[:1] if st in (Status.MET, Status.PARTIAL) else []
            items[i] = {"status": st.value, "evidence": ev, "rationale": "mock"}
        return SimpleNamespace(text=_json.dumps({"items": items}), usage_metadata=None)


def run_transcript_review(vtt_path: Path, mode: Literal["mock", "gemini"] = "mock", tier: str = "flash",
                          progress: Progress | None = None) -> dict:
    """Full review from a Zoom .vtt alone: parse -> pseudonymise + leak check -> B1 judge -> same scorer.
    Camera/background can't be judged from text: that item is not_observable and confidence is capped at medium."""
    from types import SimpleNamespace

    from cqa.aggregate import Consensus, ItemConsensus
    from cqa.modules.b1_transcript import LeakError, judge, leak_check, pseudonymise
    from cqa.observer import CallStats
    from cqa.rubric import ITEM_IDS
    from cqa.schema import CameraObs
    from cqa.scorer import score

    say = progress or (lambda _m, _f: None)
    t0 = time.monotonic()
    say("Reading the transcript", 0.1)
    turns = parse_vtt(Path(vtt_path).read_text(encoding="utf-8", errors="replace"))
    if not turns:
        raise InputError("This .vtt has no transcript lines. Upload a Zoom transcript (.vtt) with speaker cues.")
    duration = max(t.end for t in turns)
    say("Replacing children's names with Kid1, Kid2…", 0.25)
    pseudo, mapping = pseudonymise(turns)
    try:
        leak_check(pseudo, mapping["names"])
    except LeakError as exc:
        raise InputError("The transcript could not be anonymised safely, so nothing was sent for review.") from exc

    if mode == "mock":
        client, model = _MockJudgeClient(pseudo), "mock"
    else:
        client, model = _gemini_client(), model_id(tier)
    say("Scoring the transcript against the rubric", 0.5)
    res = judge(pseudo, client, model, duration)
    if not res.ok:
        raise RuntimeError(f"The transcript judge failed: {res.error}")

    evidence = {}
    for v in res.votes:
        evidence.setdefault(v.item, []).extend(v.evidence)
    statuses = res.features.get("statuses", {})
    items = {}
    for i in ITEM_IDS:
        st = Status(statuses.get(i, Status.NOT_OBSERVABLE.value)) if i != "camera_background" else Status.NOT_OBSERVABLE
        items[i] = ItemConsensus(status=st, agreement=1.0, evidence=evidence.get(i, []), votes=[st])
    a = score(Consensus(items=items, camera=CameraObs(), session_type="regular_class", session_agreement=1.0),
              flags=res.flags, transcript_only=True)
    stats = [CallStats(input_tokens=res.features.get("input_tokens", 0))] if mode != "mock" else [CallStats()]
    rep = _report(SimpleNamespace(assessment=a, runs=[], stats=stats), duration, mode, model,
                  {"b1": res}, time.monotonic() - t0)
    rep["review_type"] = "transcript"
    say("Writing the report", 0.95)
    return rep
