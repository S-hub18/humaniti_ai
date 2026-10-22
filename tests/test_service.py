import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from cqa import service
from cqa.service import InputError, fetch_input, run_assessment
from cqa.transcript import parse_vtt

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:09.000
Ms Teacher: Hello everyone, how was your day?

2
00:00:09.500 --> 00:00:10.500
Kid One: Good!

3
00:00:11.000 --> 00:00:30.000
Audio shared by Ms Teacher: Today we learn about the opposition in king endgames.

4
00:00:31.000 --> 00:00:40.000
Ms Teacher: Kid One, what is the best move here?
"""


def test_parse_vtt_roles():
    turns = parse_vtt(VTT)
    assert [t.role for t in turns] == ["teacher", "kid", "media", "teacher"]
    assert turns[0].start == 1.0 and turns[3].end == 40.0


def test_parse_vtt_empty():
    assert parse_vtt("WEBVTT\n\n") == []


def test_fetch_rejects_non_http(tmp_path):
    for bad in ("file:///etc/passwd", "/Users/x/video.mp4", "ftp://x/y.mp4", "notaurl"):
        with pytest.raises(InputError):
            fetch_input(bad, tmp_path)


def test_fetch_routes_drive_links(tmp_path):
    seen = {}

    def fake_drive(url, out):
        seen["url"] = url
        out.write_bytes(b"x" * 10)
        return out

    p = fetch_input("https://drive.google.com/file/d/abc/view?usp=sharing", tmp_path, drive_fn=fake_drive,
                    http_fn=lambda *a: pytest.fail("http used for drive"))
    assert seen["url"].startswith("https://drive.google.com") and p.exists()


def test_fetch_size_cap(tmp_path):
    def fake_http(url, out, max_bytes):
        out.write_bytes(b"x" * 2000)
        return out

    with pytest.raises(InputError):
        fetch_input("https://example.com/v.mp4", tmp_path, max_bytes=1000, http_fn=fake_http)
    assert not (tmp_path / "input_video").exists()


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("clip") / "class.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=640x360:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=180:duration=20", "-shortest", "-pix_fmt", "yuv420p", str(p)],
                   check=True)
    return p


def _temp_dirs():
    root = Path(tempfile.gettempdir())
    return {p for p in root.glob("cqa_*")}


def test_run_assessment_mock_report(clip, tmp_path, monkeypatch):
    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    steps = []
    rep = run_assessment(clip, vtt_path=vtt, mode="mock", progress=lambda m, f: steps.append(f))
    for key in ("mode", "grade", "label", "score", "confidence", "session_type", "headline", "items", "flags",
                "flag_text", "timeline", "modules", "cost_usd", "latency_s", "strengths", "improvements"):
        assert key in rep
    assert rep["mode"] == "mock" and rep["label"] in ("Poor", "Average", "Good", "Excellent")
    assert len(rep["items"]) == 7 and rep["timeline"]["duration"] == pytest.approx(20.0, abs=0.5)
    assert set(rep["modules"]) == {"b2", "xc"}
    assert steps == sorted(steps)
    assert not list(scratch.glob("cqa_*"))  # all temp dirs removed


def test_run_assessment_modules_off(clip):
    rep = run_assessment(clip, mode="mock", modules={"b2": False, "xc": False})
    assert rep["modules"] == {}


def test_run_assessment_bad_file(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_text("not a video")
    with pytest.raises(InputError):
        run_assessment(bad, mode="mock", modules={})


def test_gemini_mode_without_key(clip, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(service, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        run_assessment(clip, mode="gemini", modules={})


def test_check_public_url_blocks_private_hosts():
    from cqa.service import check_public_url

    for bad in ("http://127.0.0.1/v.mp4", "http://localhost/v.mp4", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/x.mp4", "http://[::1]/x.mp4"):
        with pytest.raises(InputError):
            check_public_url(bad)


def test_http_fetch_blocks_private_host_without_network(tmp_path):
    with pytest.raises(InputError):
        fetch_input("http://127.0.0.1:8080/v.mp4", tmp_path)


def test_no_audio_track_does_not_stop_review(tmp_path):
    clip = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    rep = run_assessment(clip, mode="mock", modules={"b2": True, "xc": False})
    assert rep["modules"]["b2"]["ok"] is False and rep["label"]


def test_gemini_mode_uses_no_disk_cache(monkeypatch):
    import sys
    import types as _t

    import google.genai  # noqa: F401  (ensure the real package is loaded before patching)
    import cqa.gemini_observer  # noqa: F401

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    fake = _t.SimpleNamespace(Client=lambda api_key, **kw: object())
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    monkeypatch.setattr(sys.modules["google"], "genai", fake, raising=False)
    obs, model = service._observer("gemini", "flash")
    assert type(obs).__name__ == "GeminiObserver" and "flash" in model


def test_http_cap_mid_stream_removes_partial_file(tmp_path, monkeypatch):
    import requests

    class Resp:
        is_redirect = False
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, n): yield b"x" * 600; yield b"x" * 600
        def close(self): pass

    monkeypatch.setattr(service, "check_public_url", lambda url: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    out = tmp_path / "v"
    with pytest.raises(InputError):
        service._http("https://example.com/v.mp4", out, max_bytes=1000)
    assert not out.exists()


def test_b1_in_mock_mode_reports_unavailable(clip):
    rep = run_assessment(clip, mode="mock", modules={"b1": True, "b2": False, "xc": False})
    assert rep["modules"]["b1"]["ok"] is False and "Gemini" in rep["modules"]["b1"]["error"]
    assert rep["label"]  # review still completes


def test_gemini_retry_progress_reaches_ui_callback(clip, monkeypatch):
    from cqa.observer import MockObserver
    from cqa.rubric import ITEM_IDS, Status

    msgs = []

    def fake_observer(mode, tier, on_retry=None):
        on_retry(1, 4, "server")  # simulate one 503 retry inside the observer
        return MockObserver(oracle={i: Status.MET for i in ITEM_IDS}), "gemini-test-flash"

    monkeypatch.setattr(service, "_observer", fake_observer)
    run_assessment(clip, mode="gemini", modules={}, progress=lambda m, f: msgs.append(m))
    assert "Gemini is busy, retrying… (attempt 1/4)" in msgs


# ---- transcript-only review ----
def test_transcript_only_review_mock(tmp_path):
    from cqa.service import run_transcript_review

    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    rep = run_transcript_review(vtt, mode="mock")
    assert rep["review_type"] == "transcript"
    items = {it["id"]: it for it in rep["items"]}
    assert items["camera_background"]["status"] == "not_observable"
    assert rep["label"] in ("Poor", "Average", "Good", "Excellent")
    assert rep["confidence"] in ("medium", "low")
    assert any("transcript" in t.lower() and "camera" in t.lower() for t in rep["flag_text"])
    for name in ("Ms Teacher", "Kid One"):
        assert name not in json.dumps(rep)  # pseudonymised before anything is shown


def test_transcript_only_review_gemini_with_fake_client(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cqa.modules.b1_transcript import B1_ITEMS
    from cqa.service import run_transcript_review

    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    items = {i: {"status": "met", "rationale": "r",
                 "evidence": [{"t_start": "00:31", "quote": "Kid1, what is the best move here?", "observation": "o"}]}
             for i in B1_ITEMS}
    resp = SimpleNamespace(text=json.dumps({"items": items}),
                           usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=90, thoughts_token_count=0))
    sent = []
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: sent.append(kw) or resp), files=None)
    monkeypatch.setattr(service, "_gemini_client", lambda: fake)
    rep = run_transcript_review(vtt, mode="gemini")
    assert rep["review_type"] == "transcript" and rep["mode"] == "gemini" and len(sent) == 1
    assert "Ms Teacher" not in str(sent[0]["contents"]) and "Kid One" not in str(sent[0]["contents"])
    assert rep["label"] == "Excellent" and rep["confidence"] == "medium"  # capped at medium
    assert {it["id"]: it["status"] for it in rep["items"]}["camera_background"] == "not_observable"


def test_transcript_only_bad_file(tmp_path):
    from cqa.service import run_transcript_review

    bad = tmp_path / "t.vtt"
    bad.write_text("WEBVTT\n\n")
    with pytest.raises(InputError):
        run_transcript_review(bad, mode="mock")


def test_review_gate_one_at_a_time():
    import threading
    import time as _time

    from cqa.service import ReviewGate

    gate = ReviewGate()
    order, waited = [], []
    with gate.slot(on_wait=lambda s: None):
        t = threading.Thread(target=lambda: gate.slot(on_wait=lambda s: waited.append(s), poll_s=0.01).__enter__() or order.append("second"))
        t.start()
        _time.sleep(0.1)
        assert order == [] and waited  # second job is waiting, told it's next
        order.append("first-done")
    t.join(2)
    assert order == ["first-done", "second"]


def test_gemini_progress_names_steps_with_eta(clip, monkeypatch):
    from cqa.observer import MockObserver
    from cqa.rubric import ITEM_IDS, Status

    class Hooked(MockObserver):
        on_call = None

        def observe(self, media, spec, idx):
            if self.on_call:
                self.on_call(spec.name)
            return super().observe(media, spec, idx)

    msgs = []
    monkeypatch.setattr(service, "_observer", lambda mode, tier, on_retry=None: (Hooked(oracle={i: Status.MET for i in ITEM_IDS}), "gemini-test-flash"))
    run_assessment(clip, mode="gemini", modules={}, progress=lambda m, f: msgs.append(m))
    text = " | ".join(msgs)
    for step in ("main video call", "opening", "closing"):
        assert step in text
    assert "about" in text  # rough ETA shown
