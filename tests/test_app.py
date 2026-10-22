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
    assert at.radio(key="cfg_mode").value.startswith("Mock")
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
    css = (Path(APP).parent / "theme.py").read_text()
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
    assert "Transcript only" in at.radio(key="source").options


def _lum(hexc):
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_wcag_aa_contrast_both_themes():
    src = Path(APP).read_text().replace("\nmain()\n", "\n")
    mod = type(pytest)("app_mod3")
    mod.__dict__["__file__"] = APP
    exec(compile(src, APP, "exec"), mod.__dict__)
    for theme, t in mod.TOKENS.items():
        pairs = [("ink", "page"), ("muted", "page"), ("ink", "surface"), ("muted", "surface"),
                 ("ink", "warn-bg"), ("ink", "info-bg"), ("ink", "mock-bg")]
        pairs += [("chip-ink", k) for k in ("met", "partial", "notmet", "na", "poor", "average", "good", "excellent")]
        pairs += [(k, "surface") for k in ("poor", "average", "good", "excellent")]  # grade word on the hero card
        for fg, bg in pairs:
            assert _ratio(t[fg], t[bg]) >= 4.5, (theme, fg, bg, round(_ratio(t[fg], t[bg]), 2))


def test_single_forced_chess_light_theme():
    import tomllib

    cfg = tomllib.loads((Path(APP).parents[1] / ".streamlit" / "config.toml").read_text())
    assert cfg["theme"]["base"] == "light" and "light" not in cfg["theme"] and "dark" not in cfg["theme"]
    src = Path(APP).read_text() + (Path(APP).parent / "theme.py").read_text()
    assert "prefers-color-scheme" not in src  # no OS-driven colour switching (the original white-on-cream bug)
    assert cfg["theme"]["textColor"].upper() == "#14212A" and cfg["theme"]["backgroundColor"].upper() == "#F7F6EE"


def test_how_it_works_page_renders():
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.switch_page("views/how_it_works.py").run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    for heading in ("How it works", "Why it is built this way", "What we tried and dropped", "How it is evaluated",
                    "Privacy and limits", "Links"):
        assert heading in text
    assert "15/21" in text and "51%" in text and "91%" in text
    assert len(at.get("graphviz_chart")) == 1


def test_main_page_links_to_how_it_works():
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    assert "Every judgement points to a moment in the class" in " ".join(m.value for m in at.markdown)


def test_sidebar_settings_persist_across_pages():
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    next(c for c in at.checkbox if c.label.startswith("Audio delivery")).uncheck().run()
    at.switch_page("views/how_it_works.py").run()
    assert not at.exception
    assert at.session_state["cfg"]["b2"] is False  # same sidebar settings carried to the other page
    assert at.session_state["cfg"]["b2"] is False


def test_each_input_choice_shows_exactly_its_own_uploaders():
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    labels = lambda: [u.label for u in at.get("file_uploader")]  # noqa: E731
    assert labels() == ["Class recording", "Zoom transcript (.vtt, optional)"]
    at.radio(key="source").set_value("Paste a link").run()
    assert labels() == ["Zoom transcript (.vtt, optional)"] and len(at.text_input) == 1
    at.radio(key="source").set_value("Transcript only").run()
    assert labels() == ["Zoom transcript (.vtt)"]  # exactly one uploader on the transcript choice
    assert not at.exception


def test_app_shows_lost_upload_message_for_empty_buffer(monkeypatch):
    import cqa.service as svc

    with pytest.raises(svc.InputError, match="upload was lost"):
        svc.check_upload_bytes(memoryview(b""), 5_000_000)
    src = Path(APP).read_text()
    assert "check_upload_bytes(data, getattr(upload, \"size\", None))" in src
