"""Class review app (Streamlit). Upload a class recording, paste a link, or upload a transcript; get a graded,
evidence-backed review.

Runs end to end in Mock mode with no API calls. Uploaded files live in a temporary directory that is deleted as
soon as the review is built.
"""

from __future__ import annotations

import html
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa.config import MODELS, free_tier, load_dotenv  # noqa: E402
from cqa.service import GATE, InputError, fetch_input, run_assessment, run_transcript_review  # noqa: E402

log = logging.getLogger("cqa.app")


class _Done(Exception):
    """Internal: transcript-only path finished inside the shared temp/lock context."""


GRADES = ["Poor", "Average", "Good", "Excellent"]
STATUS_TEXT = {"met": "Met", "partial": "Partly met", "not_met": "Not met",
               "not_applicable": "Not applicable", "not_observable": "Not observed"}
STATUS_ORDER = {"not_met": 0, "partial": 1, "met": 2, "not_observable": 3, "not_applicable": 4}
WARN_PREFIXES = ("compliance", "cap")

# One palette, forced (see .streamlit/config.toml): the 8x8 chess light theme. No OS dark-mode switching -- that
# was the original white-text-on-cream bug. All text/background pairs meet WCAG AA (>= 4.5:1; tested).
TOKENS = {
    "light": {
        "page": "#F7F6EE", "surface": "#FFFFFF", "ink": "#14212A", "muted": "#44535B", "line": "#D8D6C6",
        "met": "#2E5E3E", "partial": "#8A5A00", "notmet": "#A3263F", "na": "#5B6770", "chip-ink": "#FFFFFF",
        "poor": "#A3263F", "average": "#8A5A00", "good": "#2F5D8A", "excellent": "#2E5E3E",
        "warn-bg": "#FBEAEC", "info-bg": "#EEF1E8", "mock-bg": "#FFF4D8",
        "board-light": "#EEEED2", "board-dark": "#7A9A5E",
    },
}
GRADE_PIECE = {"Poor": "\u265f", "Average": "\u265e", "Good": "\u265c", "Excellent": "\u265b"}  # pawn, knight, rook, queen

CSS = """
<style>
:root{ %(vars)s }
html, body, [class*="css"], .stMarkdown, p, li, label{
  font-family:'Atkinson Hyperlegible','Avenir Next','Segoe UI',system-ui,sans-serif; }
.cq-display, .cq-h1, .cq-grade, .cq-section{
  font-family:'Avenir Next Condensed','Arial Narrow','Avenir Next','Segoe UI',system-ui,sans-serif; letter-spacing:-0.01em; }
.block-container{ max-width:1040px; padding-top:2rem; }
/* Streamlit fades stale elements during a rerun; keep everything crisp while a review runs */
[data-stale="true"]{ opacity:1 !important; filter:none !important; transition:none !important; }
[data-testid="stProgress"] p{ color:var(--ink) !important; font-weight:700; font-size:1.02rem; }
[data-testid="stFileUploaderDropzoneInstructions"]{ display:none !important; }
.cq-h1{ font-size:2.9rem; font-weight:900; line-height:1.02; letter-spacing:-0.02em; color:var(--ink); margin:0 0 .5rem 0; }
.cq-board{ height:12px; width:192px; border-radius:2px; margin:0 0 1.2rem 0;
  background:repeating-conic-gradient(var(--board-dark) 0 25%%, var(--board-light) 0 50%%) 0 0/12px 12px; }
.cq-sub{ color:var(--muted); font-size:1.08rem; line-height:1.5; max-width:62ch; margin:0 0 1.4rem 0; }
.cq-section{ font-size:1.25rem; font-weight:800; color:var(--ink); margin:1.8rem 0 .5rem 0; }
.cq-hero{ position:relative; overflow:hidden; display:grid; grid-template-columns:auto 1fr; align-items:center; gap:1rem 2rem; padding:1.2rem 1.4rem; margin:.4rem 0 .6rem 0;
  background:var(--surface); border:1px solid var(--line); border-left:10px solid var(--g); border-radius:6px; }
.cq-hero::after{ content:""; position:absolute; left:10px; bottom:0; width:72px; height:48px; opacity:.18; pointer-events:none;
  background:repeating-conic-gradient(var(--board-dark) 0 25%%, transparent 0 50%%) 0 0/24px 24px; }
.cq-grade{ font-size:3.6rem; font-weight:900; line-height:1; color:var(--g); }
.cq-piece{ font-size:3.4rem; line-height:1; color:var(--g); }
.cq-meta{ display:flex; flex-direction:column; gap:.35rem; }
.cq-score{ font-size:1.25rem; font-weight:700; color:var(--ink); }
.cq-conf{ align-self:flex-start; padding:.15rem .65rem; border-radius:999px; border:2px solid var(--ink); color:var(--ink); font-weight:700; font-size:.9rem; }
.cq-conf.low{ border-color:var(--notmet); color:var(--notmet); }
.cq-left{ display:flex; align-items:center; gap:1.4rem; flex-wrap:wrap; }
.cq-right{ justify-self:end; min-width:240px; }
.cq-counts{ color:var(--ink); font-size:.95rem; margin-bottom:.45rem; }
.cq-counts b{ font-size:1.05rem; }
.cq-mini{ display:grid; grid-template-columns:1fr auto; gap:.18rem .6rem; font-size:.82rem; color:var(--muted); }
.cq-mini .bar{ height:10px; border-radius:2px; background:var(--s); align-self:center; }
.cq-mini .bar.half{ background:linear-gradient(90deg,var(--s) 50%%, var(--line) 50%%); }
.cq-mini .bar.none{ background:var(--line); }
.cq-rank{ display:grid; grid-template-columns:repeat(4,1fr); max-width:520px; margin:.3rem 0 1rem 0; cursor:default; user-select:none; }
.cq-sq{ font-size:.8rem; color:var(--muted); text-align:center; padding-top:.35rem; border-top:6px solid var(--line); }
.cq-sq.on{ border-top-color:var(--g); color:var(--ink); font-weight:800; }
@media (max-width:760px){ .cq-hero{ grid-template-columns:1fr; } .cq-right{ justify-self:start; } }
.cq-flag{ display:flex; gap:.7rem; align-items:flex-start; padding:.7rem .9rem; margin:.4rem 0; border-radius:6px;
  color:var(--ink); background:var(--info-bg); border-left:6px solid var(--good); font-size:1rem; line-height:1.45; }
.cq-flag.warn{ background:var(--warn-bg); border-left-color:var(--notmet); }
.cq-flag .ic{ font-weight:800; font-size:1.1rem; line-height:1.2; }
.cq-flag.warn .ic{ color:var(--notmet); } .cq-flag .ic{ color:var(--good); }
.cq-flag .sev{ font-weight:800; margin-right:.3rem; }
.cq-badge{ display:inline-block; margin:0 0 .8rem 0; padding:.25rem .75rem; border:2px solid var(--ink); border-radius:4px; font-weight:800; color:var(--ink); }
.cq-mock{ border:2px dashed var(--partial); background:var(--mock-bg); padding:.7rem .9rem; margin:0 0 1rem 0; color:var(--ink); border-radius:6px; }
.cq-card{ background:var(--surface); border:1px solid var(--line); border-left:6px solid var(--s); border-radius:6px; padding:.85rem 1rem; margin:.55rem 0; }
.cq-card-head{ display:flex; justify-content:space-between; align-items:baseline; gap:1rem; }
.cq-card-title{ font-weight:800; font-size:1.08rem; color:var(--ink); }
.cq-weight{ color:var(--muted); font-size:.88rem; margin-left:.4rem; font-weight:400; }
.cq-chip{ font-weight:800; font-size:.85rem; padding:.18rem .6rem; border-radius:4px; color:var(--chip-ink); background:var(--s); white-space:nowrap; }
.cq-ev{ margin:.45rem 0 0 0; padding:0; list-style:none; }
.cq-ev li{ margin:.3rem 0; color:var(--ink); line-height:1.45; }
.cq-ts{ font-variant-numeric:tabular-nums; font-weight:800; margin-right:.5rem; }
.cq-quote{ color:var(--muted); font-style:italic; }
.cq-agree{ color:var(--muted); font-size:.82rem; margin-top:.3rem; }
.cq-absent{ margin:.45rem 0 0 0; color:var(--ink); font-weight:700; }
.cq-legend{ display:flex; flex-wrap:wrap; gap:.4rem 1.2rem; color:var(--ink); font-size:.92rem; margin:.3rem 0 0 0; }
.cq-legend i{ display:inline-block; width:12px; height:12px; margin-right:.35rem; vertical-align:-1px; border-radius:2px; }
.cq-foot{ color:var(--muted); font-size:.9rem; border-top:1px solid var(--line); margin-top:1.6rem; padding-top:.8rem; }
.cq-help{ color:var(--muted); font-size:.92rem; line-height:1.45; }
.cq-empty{ background:transparent; border:1px dashed var(--line); border-radius:6px; padding:.9rem 1.1rem; color:var(--muted); }
.cq-empty b{ color:var(--ink); }
[data-testid="stBaseButton-primary"]:disabled{ background:var(--met) !important; border-color:var(--met) !important;
  color:var(--chip-ink) !important; opacity:.45; }
.cq-empty ul{ margin:.4rem 0 0 1.1rem; } .cq-empty li{ margin:.2rem 0; }
@media (max-width:640px){ .cq-h1{font-size:1.9rem;} .cq-grade{font-size:2.6rem;} }
@media (prefers-reduced-motion: no-preference){ .cq-hero{ animation:cq-in .35s ease-out 1; } }
@keyframes cq-in{ from{ transform:translateY(6px); opacity:0;} to{ transform:none; opacity:1;} }
</style>
"""


def theme_css() -> str:
    return CSS % {"vars": " ".join(f"--{k}:{v};" for k, v in TOKENS["light"].items())}


def esc(s) -> str:
    """HTML-escape and collapse whitespace: a blank line would end Streamlit's HTML block and let the rest of
    model text be parsed as Markdown (links, images)."""
    return html.escape(" ".join(str(s).split()))


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def grade_var(label: str | None) -> str:
    return f"var(--{label.lower()})" if label else "var(--na)"


def hero_html(rep: dict) -> str:
    g = grade_var(rep["label"])
    counts = {}
    for it in rep["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    summary = ", ".join(f"{counts[k]} {STATUS_TEXT[k].lower()}" for k in ("met", "partial", "not_met") if counts.get(k))
    conf = esc(rep["confidence"])
    if rep["label"]:
        head = (f'<span class="cq-piece" aria-hidden="true">{GRADE_PIECE[rep["label"]]}</span>'
                f'<div class="cq-grade">{esc(rep["label"])}</div>')
        score = f'<div class="cq-score">Score {rep["score"]:.0f} of 100</div>'
    else:
        head = '<div class="cq-grade">Not graded</div>'
        score = f'<div class="cq-score">{esc(rep["headline"])}</div>'
    scale = "".join(f'<div class="cq-sq{" on" if rep["label"] == gr else ""}">{gr}</div>' for gr in GRADES)
    colour = {"met": "var(--met)", "partial": "var(--partial)", "not_met": "var(--notmet)"}
    bars = "".join(
        f'<span>{esc(it["title"])}</span><span class="bar {"half" if it["status"] == "partial" else "none" if it["status"] not in colour else ""}" '
        f'style="--s:{colour.get(it["status"], "var(--line)")};width:90px" title="{esc(STATUS_TEXT[it["status"]])}"></span>'
        for it in rep["items"])
    right = (f'<div class="cq-right"><div class="cq-counts">{esc(summary) or "No items scored"}</div>'
             f'<div class="cq-mini">{bars}</div></div>')
    return (f'<div class="cq-hero" style="--g:{g}"><div class="cq-left">{head}<div class="cq-meta">{score}'
            f'<span class="cq-conf {conf}">Confidence: {conf}</span></div></div>{right}</div>'
            f'<div class="cq-rank" style="--g:{g}" role="img" aria-label="Grade scale, {esc(rep["label"] or "not graded")} highlighted">{scale}</div>')


def flags_html(rep: dict) -> str:
    out = []
    if rep["session_type"] == "demo":
        out.append(('info', "This looks like a one-to-one trial or demo session, so it was not graded as a regular class."))
    for raw, text in zip(rep["flags"], rep["flag_text"]):
        out.append(("warn" if raw.startswith(WARN_PREFIXES) else "info", text))
    html_parts = []
    for kind, text in sorted(out, key=lambda kt: kt[0] != "warn"):
        icon, sev = ("!", "Affects the grade:") if kind == "warn" else ("i", "Note:")
        html_parts.append(f'<div class="cq-flag {kind}" role="note"><span class="ic" aria-hidden="true">{icon}</span>'
                          f'<span><span class="sev">{sev}</span>{esc(text)}</span></div>')
    return "".join(html_parts)


def item_html(it: dict) -> str:
    s = {"met": "var(--met)", "partial": "var(--partial)", "not_met": "var(--notmet)"}.get(it["status"], "var(--na)")
    ev = "".join(
        f'<li><span class="cq-ts">{esc(e["t_start"])}</span>{esc(e["observation"])}'
        + (f' <span class="cq-quote">&ldquo;{esc(e["quote"])}&rdquo;</span>' if e.get("quote") else "")
        + "</li>"
        for e in it["evidence"][:3]
    )
    if it["scored"] and it.get("votes_total"):
        agree = f'<div class="cq-agree">{it["votes_agree"]} of {it["votes_total"]} review passes agreed</div>'
    else:
        agree = ""
    absent = f'<p class="cq-absent">{esc(it["absence"])}</p>' if it.get("absence") else ""
    return (f'<div class="cq-card" style="--s:{s}"><div class="cq-card-head">'
            f'<span class="cq-card-title">{esc(it["title"])}<span class="cq-weight">weight {it["weight"]}</span></span>'
            f'<span class="cq-chip">{esc(STATUS_TEXT[it["status"]])}</span></div>'
            f'{absent}<ul class="cq-ev">{ev}</ul>{agree}</div>')


def timeline_svg(tl: dict) -> str:
    d = max(tl["duration"], 1.0)
    w, h = 1000, 64
    color = {"met": "var(--met)", "partial": "var(--partial)", "not_met": "var(--notmet)"}
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Class timeline with evidence markers">',
             f'<rect x="0" y="26" width="{w}" height="10" rx="3" fill="var(--line)"/>']
    cs, ce = tl.get("class_start"), tl.get("class_end")
    if cs is not None and ce is not None and ce > cs:
        parts.append(f'<rect x="{cs / d * w:.1f}" y="20" width="{(ce - cs) / d * w:.1f}" height="22" fill="none" '
                     f'stroke="var(--ink)" stroke-width="2" rx="3"/>')
    for m in tl["markers"]:
        x = m["t"] / d * w
        parts.append(f'<g><title>{esc(m["label"])} {esc(m["item"])}</title><line x1="{x:.1f}" x2="{x:.1f}" y1="12" y2="50" '
                     f'stroke="{color.get(m["status"], "var(--na)")}" stroke-width="5"/></g>')
    for frac in (0, 0.25, 0.5, 0.75, 1):
        x = min(max(frac * w, 20), w - 20)
        parts.append(f'<text x="{x:.1f}" y="62" font-size="13" text-anchor="middle" fill="var(--muted)">{fmt_ts(frac * d)}</text>')
    parts.append("</svg>")
    legend = ('<div class="cq-legend"><span><i style="background:var(--met)"></i>Evidence for a met item</span>'
              '<span><i style="background:var(--partial)"></i>Partly met</span>'
              '<span><i style="background:var(--notmet)"></i>Not met</span>'
              '<span><i style="border:2px solid var(--ink)"></i>Detected class start to end</span></div>')
    return "".join(parts) + legend


def render_report(rep: dict) -> None:
    if rep.get("review_type") == "transcript":
        st.markdown('<div class="cq-badge">Transcript-only review</div>', unsafe_allow_html=True)
    if rep["mode"] == "mock":
        st.markdown('<div class="cq-mock"><b>Mock result.</b> This review used the built-in demo observer, not an AI '
                    'model. Switch the review engine to Gemini for a real assessment.</div>', unsafe_allow_html=True)
    st.markdown(hero_html(rep), unsafe_allow_html=True)
    flags = flags_html(rep)
    if flags:
        st.markdown(flags, unsafe_allow_html=True)
    items = sorted(rep["items"], key=lambda it: STATUS_ORDER.get(it["status"], 9))
    todo = [it for it in items if it["status"] in ("not_met", "partial")]
    good = [it for it in items if it["status"] == "met"]
    rest = [it for it in items if it["status"] not in ("not_met", "partial", "met")]
    if todo:
        st.markdown('<div class="cq-section">What to work on</div>' + "".join(item_html(it) for it in todo),
                    unsafe_allow_html=True)
    if good:
        st.markdown('<div class="cq-section">What went well</div>' + "".join(item_html(it) for it in good),
                    unsafe_allow_html=True)
    if rest:
        st.markdown('<div class="cq-section">Not scored</div>' + "".join(item_html(it) for it in rest),
                    unsafe_allow_html=True)
    st.markdown('<div class="cq-section">Where the evidence sits in the class</div>' + timeline_svg(rep["timeline"]),
                unsafe_allow_html=True)
    mods = rep.get("modules", {})
    if mods:
        with st.expander("Details from the extra checks"):
            for name, m in mods.items():
                title = {"b1": "Transcript judge", "b2": "Audio delivery", "xc": "Camera and background frames"}.get(name, name)
                st.markdown(f"**{title}**" + ("" if m["ok"] else f" (did not run: {m['error']})"))
                st.json(m["features"], expanded=False)
    st.markdown(
        f'<div class="cq-foot">Model {esc(rep["model"])}. {rep["calls"]} calls, {rep["input_tokens"]:,} tokens in and '
        f'{rep["output_tokens"]:,} out, ${rep["cost_usd"]:.3f}, {rep["latency_s"]:.0f} s.</div>', unsafe_allow_html=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    st.set_page_config(page_title="Class review", page_icon="♞", layout="wide")
    st.markdown(theme_css(), unsafe_allow_html=True)
    load_dotenv()
    has_key = bool(os.environ.get("GEMINI_API_KEY"))

    with st.sidebar:
        st.markdown("### Review engine")
        mode = st.radio("Who reviews the class", ["Mock (no API cost)", "Gemini"], index=0,
                        help="Mock runs the whole app with a built-in demo observer.")
        st.markdown('<p class="cq-help"><b>Mock</b> shows how the review looks, using made-up observations. It is free '
                    'and instant, but the grade is not real.<br><b>Gemini</b> watches and listens to the actual class and '
                    'gives a real review.</p>', unsafe_allow_html=True)
        if mode == "Gemini" and not has_key:
            st.warning("Gemini needs GEMINI_API_KEY on the server. Using Mock instead.")
            mode = "Mock (no API cost)"
        tier = st.selectbox("Gemini model", list(MODELS), format_func=lambda t: f"{t} ({MODELS[t]})",
                            disabled=not mode.startswith("Gemini"))
        if mode == "Gemini" and free_tier():
            st.markdown('<p class="cq-help">This key is on the <b>free tier</b>: the app paces its calls and waits '
                        'out rate limits, so a one-hour class takes about 10 to 15 minutes.</p>', unsafe_allow_html=True)
        st.markdown("### Extra checks")
        b1 = st.checkbox("Transcript judge (Gemini only)", value=True, disabled=not mode.startswith("Gemini"),
                         help="Scores the transcript separately (your .vtt, or Gemini transcribes the audio). "
                              "Children's names are replaced with Kid1, Kid2… before scoring.")
        b2 = st.checkbox("Audio delivery check (runs locally)", value=True)
        xc = st.checkbox("Camera and background check (runs locally)", value=True)
        st.markdown("### Privacy")
        st.markdown('<p class="cq-help">Files are processed in a temporary folder and deleted as soon as the review is '
                    'built. Nothing is stored. In Gemini mode the recording goes to Google Gemini and is deleted there '
                    'after the review.</p>', unsafe_allow_html=True)

    st.markdown('<h1 class="cq-h1">Review a recorded chess class</h1>'
                '<p class="cq-sub">Add a class recording, a link to one, or just its Zoom transcript. You get a grade from '
                'Poor to Excellent, with timestamped evidence for each part of the 8x8 teaching rubric.</p>'
                '<div class="cq-board" aria-hidden="true"></div>',
                unsafe_allow_html=True)

    src_tab, link_tab, tx_tab = st.tabs(["Upload a recording", "Paste a link", "Transcript only"])
    with src_tab:
        upload = st.file_uploader("Class recording", type=["mp4", "mov", "mkv", "webm"], max_upload_size=600,
                                  help="MP4, MOV, MKV or WEBM, up to 600 MB.")
        st.caption("MP4, MOV, MKV or WEBM, up to 600 MB.")
    with link_tab:
        link = st.text_input("Google Drive share link or direct video link", placeholder="https://drive.google.com/file/d/...")
    with tx_tab:
        vtt_only = st.file_uploader("Zoom transcript (.vtt)", type=["vtt"], max_upload_size=5, key="vtt_only",
                                    help="No video needed. The camera and background item can't be judged from text.")
        st.caption("Review from the transcript alone. Children's names are replaced with Kid1, Kid2… before scoring.")
    vtt = st.file_uploader("Zoom transcript for a recording (.vtt, optional)", type=["vtt"], max_upload_size=5,
                           help="If you have the Zoom transcript, add it. It improves quote checking.")

    c1, c2 = st.columns([1, 2])
    go = c1.button("Review this class", type="primary", disabled=not (upload or link or vtt_only))
    sample = c2.button("Run a sample review", type="secondary")
    c2.caption("Uses a generated test video in Mock mode. No real class, no API calls.")
    if sample:
        st.session_state.pop("report", None)
        with st.spinner("Building a synthetic test video and reviewing it"):
            with tempfile.TemporaryDirectory(prefix="cqa_sample_") as tmp:
                clip = Path(tmp) / "sample.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                "testsrc=duration=60:size=640x360:rate=5", "-f", "lavfi", "-i",
                                "sine=frequency=180:duration=60", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
                st.session_state["report"] = run_assessment(clip, mode="mock", modules={"b2": b2, "xc": xc})

    if go:
        st.session_state.pop("report", None)  # never show a previous class's report under a new error
        bar = st.progress(0.0, text="Starting")
        engine = "gemini" if mode == "Gemini" else "mock"
        progress = lambda msg, f: bar.progress(min(f, 1.0), text=msg)  # noqa: E731
        try:
            with GATE.slot(on_wait=lambda s: bar.progress(0.0, text=f"Another review is running. You're next (waiting {s:.0f}s).")) \
                    if engine == "gemini" else nullcontext(), tempfile.TemporaryDirectory(prefix="cqa_upload_") as tmp:
                tmpd = Path(tmp)
                if upload is None and not link and vtt_only is not None:
                    tx = tmpd / "transcript.vtt"
                    tx.write_bytes(vtt_only.getbuffer())
                    st.session_state["report"] = run_transcript_review(tx, mode=engine, tier=tier, progress=progress)
                    raise _Done
                if upload is not None:
                    video = tmpd / f"upload{Path(upload.name).suffix.lower()}"
                    video.write_bytes(upload.getbuffer())
                else:
                    bar.progress(0.02, text="Downloading the recording")
                    video = fetch_input(link, tmpd)
                vtt_path = None
                if vtt is not None:
                    vtt_path = tmpd / "transcript.vtt"
                    vtt_path.write_bytes(vtt.getbuffer())
                st.session_state["report"] = run_assessment(
                    video, vtt_path=vtt_path, mode=engine, tier=tier,
                    modules={"b1": b1 and mode == "Gemini", "b2": b2, "xc": xc}, progress=progress)
            bar.empty()
        except _Done:
            bar.empty()
        except InputError as exc:
            bar.empty()
            st.error(str(exc))
        except RuntimeError as exc:
            bar.empty()
            st.error(f"The review could not run: {exc}")
        except Exception as exc:  # never show a stack trace to the user; log it server side
            bar.empty()
            log.exception("review failed")
            if type(exc).__name__ == "ServerError" or getattr(exc, "code", None) in (500, 502, 503, 504):
                st.error("Gemini is overloaded right now and did not recover after several retries. "
                         "Try again in a minute, or use Mock mode.")
            elif getattr(exc, "code", None) == 429:
                st.error("Gemini's rate limit was reached and did not clear after several retries. Try again in a minute.")
            else:
                st.error(f"The review stopped unexpectedly ({type(exc).__name__}). Try again, or try Mock mode.")

    rep = st.session_state.get("report")
    if rep:
        render_report(rep)
    else:
        st.markdown('<div class="cq-empty"><b>Choose one way to add the class, then select Review this class.</b><ul>'
                    '<li>Upload a recording: the full review, video and audio.</li>'
                    '<li>Paste a link: a Google Drive share link or a direct link to the video.</li>'
                    '<li>Transcript only: just the Zoom .vtt. Camera and background are not scored.</li></ul>'
                    '<p class="cq-help">Not sure yet? Run a sample review to see what a report looks like.</p></div>',
                    unsafe_allow_html=True)


main()
