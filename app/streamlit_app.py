"""Class review app (Streamlit). Upload a class recording or paste a link; get a graded, evidence-backed review.

Runs end to end in Mock mode with no API calls. Uploaded files live in a temporary directory that is
deleted as soon as the review is built.
"""

from __future__ import annotations

import html
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa.config import MODELS, load_dotenv  # noqa: E402
from cqa.service import InputError, fetch_input, run_assessment  # noqa: E402

log = logging.getLogger("cqa.app")
GRADES = ["Poor", "Average", "Good", "Excellent"]
STATUS_TEXT = {"met": "Met", "partial": "Partly met", "not_met": "Not met",
               "not_applicable": "Not applicable", "not_observable": "Not observed"}
FLAG_KIND = {"compliance": "warn", "crosscheck_disagree": "note", "audio_defect": "note",
             "unsupported_claim": "note", "improve": "note"}

CSS = """
<style>
:root{
  --ink:#1F2A2E; --muted:#56646A; --page:#F4F6F2; --sq-light:#E9EEE6; --sq-dark:#8FA68F;
  --met:#2F6F4E; --partial:#B7862A; --notmet:#9E2F45; --na:#8A949A; --line:#D5DDD3;
}
@media (prefers-color-scheme: dark){
  :root{ --ink:#E7ECE6; --muted:#A7B3AD; --page:#141A1C; --sq-light:#2A3532; --sq-dark:#4D6553;
         --met:#6FBF93; --partial:#E0B458; --notmet:#E07A8C; --na:#8E989E; --line:#2E3A37; }
}
html, body, [class*="css"], .stMarkdown, p, li, label { font-family:'Atkinson Hyperlegible', 'Avenir Next', 'Segoe UI', system-ui, sans-serif; }
h1,h2,h3,.cq-display{ font-family:'Familjen Grotesk', 'Avenir Next Condensed', 'Arial Narrow', system-ui, sans-serif; letter-spacing:-0.01em; color:var(--ink); }
.block-container{ max-width:1080px; padding-top:2.2rem; }
.cq-title{ font-size:2.4rem; font-weight:700; line-height:1.05; margin:0 0 .35rem 0; }
.cq-sub{ color:var(--muted); font-size:1.05rem; max-width:62ch; margin-bottom:1.6rem; }
.cq-rank{ display:grid; grid-template-columns:repeat(4,1fr); border:2px solid var(--ink); max-width:640px; }
.cq-sq{ aspect-ratio:1/0.62; display:flex; flex-direction:column; justify-content:flex-end; padding:.55rem .7rem;
        font-family:'Familjen Grotesk',sans-serif; font-weight:600; font-size:1rem; color:var(--ink); position:relative; }
.cq-sq:nth-child(odd){ background:var(--sq-light); } .cq-sq:nth-child(even){ background:var(--sq-dark); }
.cq-sq.on{ outline:4px solid var(--ink); outline-offset:-4px; }
.cq-piece{ position:absolute; top:.15rem; right:.5rem; font-size:2.6rem; line-height:1; color:var(--ink); }
.cq-verdict{ margin-top:1rem; font-size:1.15rem; color:var(--ink); }
.cq-verdict b{ font-family:'Familjen Grotesk',sans-serif; font-size:1.35rem; }
.cq-conf{ display:inline-block; margin-left:.6rem; padding:.1rem .55rem; border:1.5px solid var(--ink); border-radius:999px; font-size:.85rem; }
.cq-flag{ border-left:5px solid var(--partial); background:color-mix(in srgb, var(--partial) 10%, transparent); padding:.6rem .9rem; margin:.4rem 0; color:var(--ink); }
.cq-flag.warn{ border-left-color:var(--notmet); background:color-mix(in srgb, var(--notmet) 9%, transparent); }
.cq-mock{ border:2px dashed var(--partial); padding:.6rem .9rem; margin:0 0 1.2rem 0; color:var(--ink); }
.cq-item{ border-top:1px solid var(--line); padding:1rem 0 .9rem 0; display:grid; grid-template-columns:1fr auto; gap:.2rem 1rem; }
.cq-item h3{ font-size:1.12rem; margin:0; }
.cq-weight{ color:var(--muted); font-size:.9rem; }
.cq-chip{ align-self:start; font-weight:700; font-size:.88rem; padding:.18rem .6rem; border-radius:4px; color:#fff; white-space:nowrap; }
.cq-chip.met{ background:var(--met);} .cq-chip.partial{ background:var(--partial);} .cq-chip.not_met{ background:var(--notmet);}
.cq-chip.not_applicable,.cq-chip.not_observable{ background:var(--na);}
.cq-ev{ grid-column:1/-1; margin:.35rem 0 0 0; padding:0; list-style:none; }
.cq-ev li{ margin:.25rem 0; color:var(--ink); }
.cq-ts{ font-variant-numeric:tabular-nums; font-weight:700; margin-right:.5rem; }
.cq-quote{ color:var(--muted); font-style:italic; }
.cq-agree{ grid-column:1/-1; color:var(--muted); font-size:.85rem; }
.cq-foot{ color:var(--muted); font-size:.88rem; border-top:1px solid var(--line); margin-top:1.6rem; padding-top:.8rem; }
[data-testid="stFileUploaderDropzoneInstructions"] small{ display:none; }
.cq-privacy{ color:var(--muted); font-size:.88rem; max-width:62ch; }
@media (max-width:640px){ .cq-title{font-size:1.8rem;} .cq-piece{font-size:1.8rem;} .cq-sq{font-size:.85rem;} }
@media (prefers-reduced-motion: no-preference){ .cq-sq.on .cq-piece{ animation:cq-drop .5s ease-out 1; } }
@keyframes cq-drop{ from{ transform:translateY(-14px); opacity:0;} to{ transform:none; opacity:1;} }
</style>
"""


def esc(s) -> str:
    """HTML-escape and collapse whitespace: a blank line would end Streamlit's HTML block and let the rest of
    model text be parsed as Markdown (links, images)."""
    return html.escape(" ".join(str(s).split()))


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def rank_html(rep: dict) -> str:
    squares = []
    for i, g in enumerate(GRADES):
        on = rep["label"] == g
        piece = '<span class="cq-piece" aria-hidden="true">&#9822;</span>' if on else ""
        squares.append(f'<div class="cq-sq{" on" if on else ""}" role="listitem" aria-current="{str(on).lower()}">{piece}{g}</div>')
    return f'<div class="cq-rank" role="list" aria-label="Grade scale">{"".join(squares)}</div>'


def verdict_html(rep: dict) -> str:
    if rep["label"]:
        return (f'<div class="cq-verdict"><b>{esc(rep["label"])}</b> &nbsp;score {rep["score"]:.0f} of 100'
                f'<span class="cq-conf">confidence: {esc(rep["confidence"])}</span></div>')
    return f'<div class="cq-verdict"><b>Not graded.</b> {esc(rep["headline"])}</div>'


def timeline_svg(tl: dict) -> str:
    d = max(tl["duration"], 1.0)
    w, h = 1000, 70
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Class timeline with evidence markers">']
    for i in range(16):  # board-file stripe
        x = i * w / 16
        fill = "var(--sq-light)" if i % 2 == 0 else "var(--sq-dark)"
        parts.append(f'<rect x="{x:.1f}" y="22" width="{w / 16 + 0.5:.1f}" height="18" fill="{fill}" opacity="0.55"/>')
    cs, ce = tl.get("class_start"), tl.get("class_end")
    if cs is not None and ce is not None and ce > cs:
        parts.append(f'<rect x="{cs / d * w:.1f}" y="20" width="{(ce - cs) / d * w:.1f}" height="22" fill="none" stroke="var(--ink)" stroke-width="2"/>')
    color = {"met": "var(--met)", "partial": "var(--partial)", "not_met": "var(--notmet)"}
    for m in tl["markers"]:
        x = m["t"] / d * w
        parts.append(f'<g><title>{esc(m["label"])} {esc(m["item"])}</title>'
                     f'<line x1="{x:.1f}" x2="{x:.1f}" y1="14" y2="48" stroke="{color.get(m["status"], "var(--na)")}" stroke-width="4"/></g>')
    for frac in (0, 0.25, 0.5, 0.75, 1):
        x = min(max(frac * w, 18), w - 18)
        parts.append(f'<text x="{x:.1f}" y="64" font-size="12" text-anchor="middle" fill="var(--muted)">{fmt_ts(frac * d)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def item_html(it: dict) -> str:
    ev = "".join(
        f'<li><span class="cq-ts">{esc(e["t_start"])}</span>{esc(e["observation"])}'
        + (f' <span class="cq-quote">&ldquo;{esc(e["quote"])}&rdquo;</span>' if e.get("quote") else "")
        + "</li>"
        for e in it["evidence"][:3]
    )
    agree = f'{int(round(it["agreement"] * 100))}% of the review passes agreed' if it["scored"] else ""
    return (f'<div class="cq-item"><div><h3>{esc(it["title"])}</h3><span class="cq-weight">weight {it["weight"]}</span></div>'
            f'<span class="cq-chip {esc(it["status"])}">{esc(STATUS_TEXT[it["status"]])}</span>'
            f'<ul class="cq-ev">{ev}</ul><div class="cq-agree">{agree}</div></div>')


def flags_html(rep: dict) -> str:
    out = []
    if rep["session_type"] == "demo":
        out.append('<div class="cq-flag">This looks like a one-to-one trial or demo session, so it was not graded as a regular class.</div>')
    for raw, text in zip(rep["flags"], rep["flag_text"]):
        kind = FLAG_KIND.get(raw.split(":")[0], "note")
        out.append(f'<div class="cq-flag {kind}">{esc(text)}</div>')
    return "".join(out)


def render_report(rep: dict) -> None:
    if rep["mode"] == "mock":
        st.markdown('<div class="cq-mock"><b>Mock result.</b> This review used the built-in demo observer, not an AI model. '
                    'Switch to Gemini mode for a real assessment.</div>', unsafe_allow_html=True)
    st.markdown(rank_html(rep) + verdict_html(rep), unsafe_allow_html=True)
    if rep["flags"] or rep["session_type"] == "demo":
        st.markdown(flags_html(rep), unsafe_allow_html=True)
    st.markdown("### Where the evidence sits in the class")
    st.markdown(timeline_svg(rep["timeline"]), unsafe_allow_html=True)
    st.markdown("### Rubric")
    st.markdown("".join(item_html(it) for it in rep["items"]), unsafe_allow_html=True)
    mods = rep.get("modules", {})
    if mods:
        with st.expander("Module checks (transcript, audio, frames)"):
            for name, m in mods.items():
                title = {"b1": "Transcript judge", "b2": "Audio delivery", "xc": "Camera and background frames"}.get(name, name)
                st.markdown(f"**{title}**" + ("" if m["ok"] else f" (failed: {m['error']})"))
                st.json(m["features"], expanded=False)
    st.markdown(
        f'<div class="cq-foot">Model: {esc(rep["model"])} &nbsp;&nbsp; Calls: {rep["calls"]} &nbsp;&nbsp; '
        f'Tokens in/out: {rep["input_tokens"]:,}/{rep["output_tokens"]:,} &nbsp;&nbsp; Cost: ${rep["cost_usd"]:.3f} '
        f'&nbsp;&nbsp; Time: {rep["latency_s"]:.0f} s</div>', unsafe_allow_html=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    st.set_page_config(page_title="Class review", page_icon="♞", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    load_dotenv()
    has_key = bool(os.environ.get("GEMINI_API_KEY"))

    with st.sidebar:
        st.markdown("### Settings")
        mode = st.radio("Review engine", ["Mock (no API cost)", "Gemini"], index=0,
                        help="Mock runs the whole pipeline with a built-in demo observer.")
        if mode == "Gemini" and not has_key:
            st.warning("Gemini mode needs GEMINI_API_KEY on the server. Using Mock instead.")
            mode = "Mock (no API cost)"
        tier = st.selectbox("Gemini model tier", list(MODELS), format_func=lambda t: f"{t} ({MODELS[t]})",
                            disabled=not mode.startswith("Gemini"))
        b1 = st.checkbox("Transcript judge (Gemini)", value=True, disabled=not mode.startswith("Gemini"),
                         help="Scores the transcript separately (your .vtt, or Gemini transcribes the audio). "
                              "Children's names are replaced with Kid1, Kid2... before scoring.")
        b2 = st.checkbox("Audio delivery check (local)", value=True)
        xc = st.checkbox("Camera and background check (local)", value=True)
        st.markdown('<p class="cq-privacy">Files are processed in a temporary folder and deleted as soon as the review '
                    'is built. Nothing is stored. In Gemini mode the recording is sent to Google Gemini and deleted '
                    'from it after the review.</p>', unsafe_allow_html=True)

    st.markdown('<div class="cq-title cq-display">Review a recorded chess class</div>'
                '<div class="cq-sub">Upload a class recording or paste a link. You get a grade from Poor to Excellent, '
                'with timestamped evidence for each part of the 8x8 teaching rubric.</div>', unsafe_allow_html=True)

    src_tab, link_tab = st.tabs(["Upload a recording", "Paste a link"])
    with src_tab:
        upload = st.file_uploader("Class recording", type=["mp4", "mov", "mkv", "webm"], max_upload_size=600,
                                  help="MP4, MOV, MKV or WEBM, up to 600 MB.")
        st.caption("MP4, MOV, MKV or WEBM, up to 600 MB.")
    with link_tab:
        link = st.text_input("Google Drive share link or direct video link", placeholder="https://drive.google.com/file/d/...")
    vtt = st.file_uploader("Zoom transcript (.vtt, optional)", type=["vtt"], max_upload_size=5,
                           help="If you have the Zoom transcript, add it. It improves quote checking.")

    c1, c2 = st.columns([1, 2])
    go = c1.button("Review this class", type="primary", disabled=not (upload or link))
    sample = c2.button("Run a sample review", help="Generates a short synthetic test video and reviews it in Mock mode. "
                       "No real class, no API calls.")
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
        try:
            with tempfile.TemporaryDirectory(prefix="cqa_upload_") as tmp:
                tmpd = Path(tmp)
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
                rep = run_assessment(video, vtt_path=vtt_path, mode="gemini" if mode == "Gemini" else "mock",
                                     tier=tier, modules={"b1": b1 and mode == "Gemini", "b2": b2, "xc": xc},
                                     progress=lambda msg, f: bar.progress(min(f, 1.0), text=msg))
            bar.empty()
            st.session_state["report"] = rep
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
        st.info("Add a recording above, then select Review this class. A one-hour class takes a few minutes.")


main()
