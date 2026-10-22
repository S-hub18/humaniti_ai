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


def test_run_assessment_mock_report(clip, tmp_path):
    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    before = _temp_dirs()
    steps = []
    rep = run_assessment(clip, vtt_path=vtt, mode="mock", progress=lambda m, f: steps.append(f))
    for key in ("mode", "grade", "label", "score", "confidence", "session_type", "headline", "items", "flags",
                "flag_text", "timeline", "modules", "cost_usd", "latency_s", "strengths", "improvements"):
        assert key in rep
    assert rep["mode"] == "mock" and rep["label"] in ("Poor", "Average", "Good", "Excellent")
    assert len(rep["items"]) == 7 and rep["timeline"]["duration"] == pytest.approx(20.0, abs=0.5)
    assert set(rep["modules"]) == {"b2", "xc"}
    assert steps == sorted(steps)
    assert _temp_dirs() <= before  # all temp dirs removed


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

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    fake = _t.SimpleNamespace(Client=lambda api_key, **kw: object())
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    monkeypatch.setattr(sys.modules["google"], "genai", fake, raising=False)
    obs, model = service._observer("gemini", "flash")
    assert type(obs).__name__ == "GeminiObserver" and "flash" in model
