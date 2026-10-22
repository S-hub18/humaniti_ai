"""How it works: the whole story for graders, in the app. Content mirrors docs/design/system-design.md and
docs/writeup.md; no new claims, no names."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from theme import TOKENS, page_link, theme_css  # noqa: E402

REPO_URL = "https://github.com/S-hub18/humaniti_ai"
P = TOKENS["light"]

DIAGRAM = f"""
digraph G {{
  rankdir=LR; bgcolor="transparent"; pad=0.2; nodesep=0.3; ranksep=0.35;
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=15, color="{P['line']}",
        fillcolor="{P['surface']}", fontcolor="{P['ink']}", penwidth=1.2];
  edge [color="{P['muted']}", arrowsize=0.7];
  input [label="Input\\nvideo, link or .vtt", fillcolor="{P['board-light']}"];
  ingest [label="Ingest + timeline\\nduration, audio, class bounds"];
  subgraph cluster_core {{
    label="Gemini Flash observer"; fontname="Helvetica"; fontsize=15; fontcolor="{P['ink']}";
    color="{P['met']}"; style="rounded";
    main [label="Full class pass\\n(low resolution)"];
    focus [label="Opening + closing\\n10-minute passes"];
    vote [label="k passes,\\nmajority vote per item"];
    main -> vote; focus -> vote;
  }}
  subgraph cluster_mod {{
    label="Modules"; fontname="Helvetica"; fontsize=15; fontcolor="{P['ink']}"; color="{P['line']}"; style="rounded";
    b1 [label="B1 transcript judge\\n(names pseudonymised)"];
    b2 [label="B2 audio delivery\\n(local, notes only)"];
    xc [label="XC camera + 8x8 check\\n(local)"];
  }}
  validate [label="Evidence validation\\ntimestamps + quotes"];
  scorer [label="Deterministic scorer\\nweights, caps, gates, cutpoints", fillcolor="{P['board-light']}"];
  report [label="Report\\ngrade, confidence, evidence", fillcolor="{P['met']}", fontcolor="#FFFFFF", color="{P['met']}"];
  input -> ingest; ingest -> main; ingest -> focus; ingest -> b1; ingest -> b2; ingest -> xc;
  vote -> validate; b1 -> validate; validate -> scorer; b2 -> scorer; xc -> scorer; scorer -> report;
}}
"""


def section(title: str, body_html: str) -> None:
    st.markdown(f'<div class="cq-section">{title}</div><div class="cq-prose">{body_html}</div>', unsafe_allow_html=True)


def main() -> None:
    pages = st.session_state.get("_pages")
    if pages:  # run inside the app's navigation (normal case)
        if st.button("← Back to the review", type="tertiary"):
            st.switch_page(pages["review"])
    else:  # run standalone
        st.markdown(theme_css(), unsafe_allow_html=True)
    st.markdown('<h1 class="cq-h1">How it works</h1>'
                '<p class="cq-sub">What the app does with a class recording, why it is built this way, and how far its '
                'results can be trusted.</p><div class="cq-board" aria-hidden="true"></div>', unsafe_allow_html=True)

    st.markdown('<div class="cq-section">The pipeline</div>', unsafe_allow_html=True)
    st.graphviz_chart(DIAGRAM, use_container_width=True)

    section("Why it is built this way", """<ul>
<li><b>The rubric comes from the reviewers.</b> The academy's own feedback on the sample classes was turned into a
7-item checklist: calling on each child, rapport and introduction, energy and warmth, camera and the 8x8 background,
homework, the closing, and giving new joiners time to introduce themselves.</li>
<li><b>The AI observes, code decides.</b> Gemini marks each item met, partly met or not met, with a timestamp and a
short quote. A fixed scorer turns that checklist into the grade, so every grade traces back to evidence.</li>
<li><b>Evidence is checked.</b> Timestamps outside the recording or outside a pass's window are dropped, and quotes are
matched against the transcript when one is available. A claim with no surviving evidence does not count.</li>
<li><b>Caps for clear failures.</b> No branded background or a teacher whose face is rarely visible caps the grade at
Poor; two items not met cap it at Average, three at Poor. A disagreement between the AI and the local check lowers
confidence instead of capping.</li>
<li><b>Several passes, then a vote.</b> The observer runs more than once and each item takes the majority; how often
the passes agree becomes the confidence.</li></ul>""")

    section("What we tried and dropped", """<ul>
<li><b>A small local classifier (laya).</b> On 21 item labels taken from the reviewers' notes it scored 15/21, the
same as always guessing the most common answer, and it cannot explain itself. Dropped.</li>
<li><b>A separate speech-to-text service.</b> Dropped so only one external provider sees the recording; Gemini
transcribes when no .vtt is given.</li>
<li><b>A second AI provider for judging.</b> Dropped for the same reason; one provider keeps data handling simple.</li>
<li><b>Audio "liveliness" as a vote.</b> It rated every sample class as lively, so it does not separate classes. It
stays off; it only adds notes such as low audio level.</li></ul>""")

    section("How it is evaluated", """<ul>
<li>Against 10 labelled classes: exact grade, within-one grade, mean error, rank correlation and pairwise ordering,
each next to a "most common grade" baseline, with leave-one-out tuning of the grade cutpoints. Two classes are held
out and scored once.</li>
<li>Per-item accuracy against the reviewers' notes, and separately against our own transcript annotations.
Careful human annotation from transcripts agreed with the reviewers 51% exactly and 91% within one step, which is a
realistic ceiling.</li>
<li>Counterfactual video edits (cut the homework, black out the camera tile, trim the tail) check that the right items
move.</li>
<li><b>Honest status:</b> the live result so far is a single class (B472, rated Excellent by the reviewers; the app
gave Excellent, about 95/100). Grade-level accuracy rests on 10 labelled classes, so treat all numbers as
indicative.</li></ul>""")

    section("Privacy and limits", """<ul>
<li>The recordings show children. Uploads go to a temporary folder and are deleted as soon as the review is built;
nothing is stored.</li>
<li>Children's names are replaced with Kid1, Kid2… before any transcript is scored, and scoring stops if a name
survives. The model is told never to name or describe children.</li>
<li>In Gemini mode the recording is sent to Google Gemini and deleted there after the review. The current key is on
the free tier, whose terms may allow data use; production use needs a paid tier and the organiser's approval.</li>
<li>The free tier is slow: the app paces its calls and waits out rate limits, so a one-hour class takes about 10 to 15
minutes.</li></ul>""")

    section("Links", f"""<ul>
<li>Source code: <a href="{REPO_URL}">{REPO_URL}</a></li>
<li>Design and writeup: <code>docs/design/system-design.md</code> and <code>docs/writeup.md</code> in the repository.</li>
</ul>""")


main()
