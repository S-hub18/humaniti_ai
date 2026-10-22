"""Service layer used by the app: fetch input, run the assessment end to end, build a JSON report.

Privacy: every temporary file this module creates (downloaded video, extracted audio, sampled
frames) lives in a temp dir that is deleted in `finally`. Transcript text is never logged.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from cqa.config import load_dotenv, model_id
from cqa.evidence import parse_ts
from cqa.explain import build_explanation
from cqa.media import probe
from cqa.modules import ModuleResult
from cqa.observer import MockObserver
from cqa.pipeline import PipelineConfig, assess
from cqa.rubric import ITEMS, STATUS_VALUE, Status
from cqa.schema import TranscriptTurn
from cqa.transcript import parse_vtt

Progress = Callable[[str, float], None]
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
                fh.write(chunk)
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


def _observer(mode: str, tier: str):
    if mode == "mock":
        return MockObserver(oracle=MOCK_ORACLE, noise=0.1, seed=7), "mock"
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Gemini mode needs GEMINI_API_KEY. Set it in the environment, or use Mock mode.")
    from google import genai

    from cqa.gemini_observer import GeminiObserver

    # No CachedObserver here: the app must not persist observations about a class (cache is eval-only).
    model = model_id(tier)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return GeminiObserver(client, model=model), model


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
        try:
            media = probe(video_path)
        except Exception as exc:  # ffprobe failure = not a playable video
            raise InputError("Could not read this file as a video. Upload an mp4, mov, mkv or webm recording.") from exc
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
        observer, model = _observer(mode, tier)
        votes = [v for r in results.values() for v in r.votes] if b2_vote else []
        gates = [g for r in results.values() for g in r.gates]
        flags = [f for r in results.values() for f in r.flags]
        run = assess(media, observer, PipelineConfig(), transcript=transcript,
                     module_votes=votes, gates=gates, extra_flags=flags)
        say("Writing the report", 0.95)
        return _report(run, media.duration, mode, model, results, time.monotonic() - t0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    }
