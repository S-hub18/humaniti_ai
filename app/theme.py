"""Shared 8x8 chess light theme for every page (palette, CSS)."""

from __future__ import annotations

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
.cq-points{ display:flex !important; flex-wrap:wrap; gap:.3rem 1.4rem; list-style:none; padding:0; margin:-.6rem 0 1rem 0; color:var(--ink); font-size:.98rem; }
.cq-points li{ margin:0 !important; }
.cq-points li::before{ content:"\\25A0"; color:var(--board-dark); margin-right:.45rem; font-size:.7rem; vertical-align:2px; }
.cq-prose{ color:var(--ink); max-width:72ch; line-height:1.6; font-size:1.02rem; }
.cq-prose li{ margin:.3rem 0; }
.cq-board{ height:12px; width:192px; border-radius:2px; margin:0 0 1.2rem 0;
  background:repeating-conic-gradient(var(--board-dark) 0 25%%, var(--board-light) 0 50%%) 0 0/12px 12px; }
.cq-sub{ color:var(--muted); font-size:1.08rem; line-height:1.5; max-width:62ch; margin:0 0 1.4rem 0; }
.cq-section{ font-size:1.25rem; font-weight:800; color:var(--ink); margin:1.8rem 0 .5rem 0; }
.cq-hero{ position:relative; overflow:hidden; display:grid; grid-template-columns:auto 1fr; align-items:center; gap:1rem 2rem; padding:1.2rem 1.4rem; margin:.4rem 0 .6rem 0;
  background:var(--surface); border:1px solid var(--line); border-left:10px solid var(--g); border-radius:6px; }
.cq-hero > *{ position:relative; z-index:1; }
.cq-hero::after{ content:""; position:absolute; z-index:0; left:10px; bottom:0; width:72px; height:48px; opacity:.18; pointer-events:none;
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




def page_link(page: str, label: str, href: str) -> None:
    """st.page_link, falling back to a plain link when the page registry isn't available (e.g. a page run alone)."""
    import streamlit as st

    try:
        st.page_link(page, label=label)
    except Exception:
        st.markdown(f'<a href="{href}" target="_self">{label}</a>', unsafe_allow_html=True)
