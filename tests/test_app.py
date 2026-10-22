import html
import subprocess
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")


def test_app_renders_in_mock_mode(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    assert at.radio[0].value.startswith("Mock")
    assert at.button[0].disabled  # nothing uploaded yet


def test_render_report_from_mock_service(tmp_path):
    from cqa.service import run_assessment

    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=12:size=640x360:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=12", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
    rep = run_assessment(clip, mode="mock", modules={"b2": True, "xc": False})

    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.session_state["report"] = rep
    at.run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "Mock result" in page and rep["label"] in page and "cq-rank" in page
    assert all(html.escape(it["title"]) in page for it in rep["items"])


def test_model_text_is_html_escaped():
    import importlib.util

    spec = importlib.util.spec_from_file_location("app_mod", APP)
    src = Path(APP).read_text().replace("\nmain()\n", "\n")
    mod = type(importlib)("app_mod")
    mod.__dict__["__file__"] = APP
    exec(compile(src, APP, "exec"), mod.__dict__)
    it = {"title": "Homework", "weight": 12, "status": "met", "scored": True, "agreement": 1.0,
          "evidence": [{"t_start": "01:00", "observation": "<script>alert(1)</script>", "quote": "<img src=x onerror=1>"}]}
    out = mod.item_html(it)
    assert "<script>" not in out and "<img" not in out and "&lt;script&gt;" in out


def test_sample_review_button_renders_a_mock_report(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    at = st_testing.AppTest.from_file(APP, default_timeout=180)
    at.run()
    sample = next(b for b in at.button if b.label == "Run a sample review")
    sample.click().run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "Mock result" in page and "cq-rank" in page


def test_esc_collapses_blank_lines():
    src = Path(APP).read_text().replace("\nmain()\n", "\n")
    mod = type(pytest)("app_mod2")
    mod.__dict__["__file__"] = APP
    exec(compile(src, APP, "exec"), mod.__dict__)
    assert "\n" not in mod.esc("line one\n\n![x](http://evil/t.png)")


def test_uploader_native_size_label_hidden():
    css = Path(APP).read_text()
    assert '[data-testid="stFileUploaderDropzoneInstructions"]{ display:none' in css


def test_transcript_only_report_shows_badge():
    from cqa.service import run_transcript_review

    vtt = Path(APP).parent.parent / "tests" / "_tmp_t.vtt"
    vtt.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nTeach Er: Hello everyone, how was your day?\n\n"
                   "2\n00:00:06.000 --> 00:00:07.000\nSome Kid: Good!\n")
    try:
        rep = run_transcript_review(vtt, mode="mock")
    finally:
        vtt.unlink()
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.session_state["report"] = rep
    at.run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "Transcript-only review" in page
    assert any(t.label == "Transcript only" for t in at.tabs)
