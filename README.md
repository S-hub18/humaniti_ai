---
title: Class Review
emoji: ♞
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Class Review: grading recorded online chess classes

Upload a recording of an online kids' chess class (or paste a Google Drive / direct link). The app grades it
**Poor / Average / Good / Excellent** and backs the grade with timestamped evidence for each part of the
teaching rubric.

## How it works

```
recording ─► local ingest (ffprobe, audio, sampled frames)
          ─► Gemini Flash observer: main pass over the full video + audio, plus higher-detail
             passes on the first and last 10 minutes; repeated 3×, majority vote per rubric item
          ─► modules: transcript judge (B1: .vtt or Gemini audio transcription → kids pseudonymised →
             Gemini text scoring, quotes verified), audio delivery (B2, local), camera / 8x8 background
             crosscheck (XC, local)
          ─► deterministic scorer in code: evidence validation, weights, compliance gates,
             demo detection, cutpoints  ─►  grade + confidence + evidence
```

- **The model observes; code grades.** Gemini fills in a 7-item checklist taken from the academy's own
  reviewer feedback: individual calling, rapport and intro, energy and warmth, camera and branded background,
  homework, conclusion, and new joiners introduced. Each item is met / partial / not met, with timestamps and
  quotes. A deterministic scorer then turns the checklist into the grade.
- **Evidence is checked.** Timestamps outside the recording or outside the pass's window are dropped. Quotes
  are matched against the transcript when one is available. Claims with no surviving evidence don't count.
- **Compliance gates.** A missing branded background, or a teacher whose face is rarely visible, caps the
  grade at Poor. The local crosscheck can only make these checks stricter.
- **Transcript judge (B1).** In Gemini mode, the transcript is scored separately (your Zoom `.vtt`, or Gemini
  transcribes the audio in 10-minute chunks). Children are replaced with Kid1, Kid2… locally; a fail-closed check
  blocks scoring if any known name survives. Its verified judgements add one vote per item.
- **Demos are detected.** One-to-one trial classes are reported as observations, not graded.

Full design: [`docs/design/system-design.md`](docs/design/system-design.md). Implementation plan:
[`docs/plans/`](docs/plans/).

## Run locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and ffmpeg.

```bash
uv sync
uv run streamlit run app/streamlit_app.py
```

The app opens in **Mock mode**. This runs the whole pipeline with a built-in demo observer and makes no API
calls, which is useful for trying the interface. Mock results are labelled as such.

### Gemini mode

Set a key in the environment or in a local `.env` file (git-ignored):

```bash
GEMINI_API_KEY=...            # required for Gemini mode
CQA_GEMINI_MODEL=...          # optional override; Flash / Flash-Lite models only
```

Only Gemini **Flash** and **Flash-Lite** models are allowed (default `gemini-3.8-flash`; tier `flash-lite` =
`gemini-3.5-flash-lite`). Any Pro model is rejected in code.

### Docker / Hugging Face Spaces

```bash
docker build -t class-review .
docker run -p 7860:7860 -e GEMINI_API_KEY=... class-review
```

On Hugging Face Spaces, add `GEMINI_API_KEY` as a Space secret. The upload limit is 600 MB
(`.streamlit/config.toml`).

## Evaluation

```bash
uv run pytest -q                                  # unit + integration tests (no API calls)
uv run python -m eval.run_eval --observer mock    # harness smoke run (numbers are NOT results)
uv run python -m eval.run_eval --observer gemini  # real eval (costs API credits)
uv run python -m eval.run_eval --observer gemini --b1   # + B1 transcript judge, adds a core+b1 ablation row
uv run python -m eval.variants --list             # metamorphic / invariance variants
```

The harness reports:
- exact accuracy, within-one accuracy and MAE against the majority-class baseline;
- Spearman correlation and pairwise ordering;
- leave-one-out cutpoint tuning;
- run-to-run variance;
- per-item accuracy against grader-derived labels and our own transcript annotations (kept separate);
- cost and latency;
- an ablation table, and out-of-distribution demo checks.

Two classes are held out and scored once.

**Limits, stated plainly:** grade-level accuracy rests on **10 labelled classes**. Item-level, pairwise and
variant results are supporting evidence only.

## Privacy and data handling

- Recordings show children. They are never committed: `class recordings*/`, `.scratch/` and `.env*` are
  git-ignored and docker-ignored.
- Uploads are written to a temporary folder and deleted as soon as the review is built. Nothing is stored or
  cached by the app.
- Audio and frame checks run locally. In Gemini mode the recording is uploaded to the Gemini API (paid tier)
  and deleted from it after the review. The model is instructed never to name or describe children.
- Transcript text is never logged.
