# Class Quality Assessor: System Design (v4, final architecture)

Status: approved architecture, build in progress. The exploration that led here (component designs, privacy debate, the laya experiment, the bake-off idea) is in `exploration-v3.md`. This document supersedes it wherever the two disagree.

## 1. Goal

Input: a recording of an 8x8 online chess class for kids (mp4, optionally with a Zoom `.vtt`).

Output:
- a grade: Poor, Average, Good or Excellent, or no grade for a demo or an abstention;
- a status for each rubric item, with timestamped evidence;
- compliance flags;
- a confidence level;
- a short explanation grounded in that evidence.

The grader feedback in `feedbacks.pdf` is the rubric. **The LLM observes and never chooses the grade.** A deterministic scorer in code turns the observations into the grade.

Providers: Gemini (paid tier) is the only external service. Every uploaded file is deleted right after use. The only key is `GEMINI_API_KEY`.

## 2. Architecture

```
mp4 (+ optional .vtt)
  │
  ├─ ingest (local): ffprobe duration, sha256, extract audio, find class bounds (first/last speech)
  │
  ├─ CORE: Gemini Flash observer (model id in config; Flash / Flash-Lite only), self-consistency k=3
  │     main call     full video+audio, media_resolution=LOW, ~1 fps   → all 7 items + session type + class bounds
  │     opening call  [start, start+10 min], media_resolution=HIGH      → rapport_intro, new_joiner_intro, camera_background
  │     closing call  [end-10 min, end],     media_resolution=HIGH      → closing, homework, camera_background
  │     → per run: merge the 3 calls into one item set (§4.2)
  │     → across the 3 runs: majority vote per item; agreement becomes item confidence
  │
  ├─ MODULES (pluggable, toggled in config; each one is an extra voter or a gate signal)
  │     B1 transcript scorer   .vtt, or a Gemini transcription → pseudonymise → Gemini text → item votes
  │     B2 audio signal        local ffmpeg/numpy → energy vote, defect flags
  │     XC local crosscheck    local face/logo/camera/luma on sampled frames → gate signals, disagreement flags
  │
  ├─ SCORER (deterministic code): evidence validation → item values → weighted score
  │                               → gates → session type → cutpoints → grade + confidence
  │
  └─ REPORT: JSON → UI; explanation is template-assembled from validated evidence
```

## 3. Rubric checklist

Seven items, taken from the grader lines. Weights are fixed priors and are never fitted to the labels.

| id | Item | Weight | Grader anchors (✓ = praised, ✗ = criticised) |
|---|---|---|---|
| `individual_calling` | Calls on kids individually by name; includes everyone | 20 | ✓ B491, B472, B435g, B516, B435a, B482 |
| `rapport_intro` | Warm opening: greets kids individually, small talk, introduces the session | 18 | ✓ B472, B552, B435g; ✗ B558, B482, B441 |
| `energy_warmth` | Energetic, pleasant, happy delivery (tone, praise, smiles) | 18 | ✗ B552 (partial), B435a, B558, B392, B441 |
| `camera_background` | Teacher camera on, face visible, 8x8 branded background, adequate light | 14 | ✓ B552; ✗ B392 (no background), B441 (no face), B482 (camera off at times), B435a (light), B516 (clarity) |
| `homework` | Homework or practice assigned (anywhere in the class) | 12 | ✓ B472, B552, B516; ✗ B491, B435g, B435a |
| `closing` | Proper conclusion: recap and goodbye | 10 | ✗ B558 |
| `new_joiner_intro` | New joiners are given time to self-introduce | 8 | ✗ B558, B482 |

Item status values: `met` = 1.0, `partial` = 0.5, `not_met` = 0.0, `not_applicable` and `not_observable` = null (left out of the score; weights are renormalised). For `new_joiner_intro`, the answer is `not_applicable` when nobody joins for the first time.

`camera_background` also returns sub-observations that drive the gates:
- `branded_background`: present, absent or unclear
- `face_visible`: mostly, sometimes, rarely or unclear
- `camera_on_while_teaching`: mostly, sometimes, rarely or unclear
- `lighting`: ok, dim or unclear

## 4. Core

### 4.1 Gemini observer calls

Every call uses:
- a **Flash-tier model only** (hard constraint: no Pro anywhere). The model ID is a config value, `cqa/config.py`, overridable with env `CQA_GEMINI_MODEL`. The defaults are `flash` = `gemini-3.8-flash` and `flash-lite` = `gemini-3.5-flash-lite`. Any model ID containing "pro" is rejected in code;
- `response_mime_type=application/json` and a `response_schema` generated from the pydantic models;
- temperature 0.4. Self-consistency needs sampling diversity; temperature 0 would make all 3 runs identical and the agreement meaningless. The value is configurable and checked in the eval.

The prompt is versioned (`PROMPT_VERSION`). It contains:
- the item definitions with anchors for met, partial and not_met;
- the domain primer: chess class, an 8x8 branded virtual background, screen share with a small speaker tile, silent arena play is legitimate, played lesson videos are not the teacher;
- the rule: "cite MM:SS timestamps and short verbatim quotes, never name or describe children".

Each item in the output has these fields: `{status, evidence: [{t_start, t_end, modality: visual|audio|both, observation, quote?}], rationale}`.

The main call also returns:
- `session_type`: regular_class, demo_trial, not_a_class or unknown;
- `class_start` and `class_end`, both in `MM:SS`;
- `teacher_description`, with no name.

The focus calls use `start_offset` and `end_offset` on the same uploaded file. The file is uploaded once per assessment and deleted in a `finally` block.

### 4.2 Merging the calls within one run (fixed rule)

| Item | Source |
|---|---|
| `rapport_intro`, `new_joiner_intro` | opening call; main call if the opening result is null |
| `closing` | closing call; main call if the closing result is null |
| `homework` | most positive status among the three calls that still has validated evidence (homework can be anywhere: B472 gave it at 45–47 min of a 56-min class) |
| `camera_background` and its sub-observations | most conservative non-null value across the calls |
| `individual_calling`, `energy_warmth` | main call |

### 4.3 Self-consistency

- Run the whole observer 3 times, with k=3 configurable.
- For each item, take the majority vote over the merged statuses.
- If all three runs differ, take the median on the ordinal scale not_met < partial < met.
- `item_agreement` = (count of the majority status) / k.
- Evidence comes from the runs that agree with the majority, de-duplicated so that items within 10 s of each other count once.

### 4.4 Evidence validation (code, before scoring)

Validation drops:
- evidence whose `t_start` falls outside [0, duration], or outside the call's offset window;
- a quote that doesn't fuzzy-match the transcript within ±20 s, when a transcript is available (token-set ratio ≥ 0.6).

A `met` or `partial` status left with no valid evidence becomes `not_observable` and is flagged `unsupported_claim:<item>`. A `not_met` needs a rationale, but evidence is optional, because absence often has no timestamp.

## 5. Scorer (deterministic, `cqa/scorer.py`)

1. **Session type:**
   - The majority vote across runs gives the session type.
   - `demo_trial` gives `grade = null`, `session_type = demo`, observations only.
   - `not_a_class` or `unknown` with agreement below 2/3 makes the scorer abstain.
2. **Coverage:** if the non-null items cover less than 60% of the total weight, abstain with reason `insufficient_observations`.
3. **Score:** `score = 100 * Σ w_i·v_i / Σ w_i` over non-null items.
4. **Gates.** Gates only ever lower the grade.
   - `branded_background == absent` caps the grade at Poor and flags `compliance:no_branded_background` (B392).
   - `face_visible == rarely` caps the grade at Poor and flags `compliance:face_not_visible` (B441).
   - `camera_on_while_teaching == sometimes` adds an improvement flag but no cap (B482).
5. **Cutpoints** are priors set before any eval: Poor below 50; Average 50 to under 68; Good 68 to under 84; Excellent 84 and above. Leave-one-out tuning may move each cutpoint by at most ±8. The tuned values are adopted only if they beat the priors by at least one exact hit.
6. **Confidence:**
   - high: mean item agreement ≥ 0.9, the score is more than 4 points from the nearest cutpoint, there is no crosscheck disagreement and no unsupported claim;
   - low: mean item agreement < 0.7, or a gate verdict is unclear;
   - medium: everything else.
7. **Explanation:** assembled from templates using the validated evidence: strengths are items that are met, improvements are items that are not_met or partial, and each line cites `MM:SS`. An optional Gemini text call may polish the wording, but it receives only the validated evidence, and its output is checked to contain the same timestamps.

## 6. Modules (each is a voter or a gate signal; on or off per config)

| Module | Input | Output into the scorer | Default |
|---|---|---|---|
| **B1 transcript** | `.vtt` if uploaded, otherwise a Gemini audio transcription (10-minute chunks with 30 s overlap, role labels teacher/kid/media/other, starts snapped to VAD onsets); names replaced locally with Kid1..N, then Gemini text judges the 6 non-visual items | one extra vote per item, weight `w_b1` (default 1 vote against the core's 3), plus deterministic features (kid turns/min in teaching segments, distinct kids addressed, time to first speech) | on |
| **B2 audio** | 16 kHz mono audio plus teacher turns | one `energy_warmth` vote from teacher liveliness (pitch range and loudness dynamics, both against corpus percentiles); flags `audio_defect:*`. Zoom gates out noise, so SNR is ignored | off until the eval shows it helps |
| **XC crosscheck** | about 120 frames sampled locally; teacher tile found by name label and speaker | gate sub-observations; if XC and the core disagree, flag `crosscheck_disagree:<field>` and drop confidence one notch; XC overrides the core only toward the conservative side, and only with at least 8 usable frames | on |

All modules write into a common `ModuleVote` structure (see the plan), so ablation is just recomputing the scorer over cached votes with modules switched on or off. No new API calls are needed.

## 7. Eval harness (built alongside the core)

The harness lives in `eval/` and is driven by `eval/dataset.yaml`. It contains:
- 10 graded classes: excellent, good and average map to their own grade, and weak and quality-issue both map to Poor;
- 2 demos as out-of-distribution tests;
- a `holdout` flag on B552 and B482. These two were fixed before any prompt work and are scored once, at the very end. Prompt iteration only ever looks at the other 8.

Item ground truth (`eval/ground_truth_items.csv`) was transcribed from the grader lines before any model output, as Y, N or partial per item. Where a grader line is silent, the cell is blank and excluded.

| Report | How |
|---|---|
| Grade accuracy | exact, within-1, MAE on 0–3, Spearman, confusion matrix, majority-class baseline |
| LOO cutpoints | 10 folds, ±8 window; prior vs tuned shown side by side |
| Run variance | grade flip rate and score SD across R=2 independent assessments, using samples 0–2 and 3–5 of a 6-sample cache, so it adds no extra cost |
| Item accuracy | per item, strict (status = GT) and lenient (partial counts toward either neighbour), with n per item |
| Cost / latency | from `usage_metadata` and wall-clock per call and per assessment: $ per class, p50 and max seconds |
| Ablation | core; core+B1; core+B2; core+XC; all; and B1-only (on vtt and on a Gemini transcript); all reported on the same metrics |
| OOD | the English demo should come out `demo`; the Malayalam demo should come out `demo` or abstain |

### 7.1 Getting more signal out of 10 classes (honestly)

**Grade-level accuracy still rests on only 10 real labels.** Everything below adds signal about the *mechanism*: does each item respond to the right evidence? None of it measures grade accuracy, and the writeup must not present it as if it did.

1. **Silver item labels (ours, not the graders').**
   - We annotate all 7 items for all 10 classes ourselves from transcript evidence: status, `MM:SS`, and a quote. That is about 70 labels, against about 21 grader-derived ones.
   - They go in `eval/silver_items.csv` with `source=ours`, kept separate from the grader ground truth (`ground_truth_items.csv`, `source=grader`). The two are reported separately and never merged.
   - The silver labels are written from the transcripts before we look at any model output for that class. Where our label disagrees with the grader's line, both are listed.
2. **Metamorphic and counterfactual tests.** Each test makes an ffmpeg variant whose expected *direction* is known in advance:

   | Variant | Source | Expectation |
   |---|---|---|
   | `trim_opening` | cut the first 5–8 min of the class from an Excellent class (B472, B491) | `rapport_intro` drops (status lower, or score Δ < 0) |
   | `cut_homework` | remove the homework segment (B472, 45:00–47:30 ± margin; B552, B516) | `homework` goes from met to not_met or partial |
   | `blackout_tile` | black box over the speaker-tile region for the whole video (any class with a branded background) | gate fires (`face_not_visible`); grade capped at Poor |
   | `cut_tail` | remove the post-class tail (B441), or pad 5 min of silence or black screen at the end | grade unchanged; \|Δscore\| ≤ 5 |

   We report the share of variants whose direction is correct, and the size of each change. The variant recipes are code (`eval/variants.py`); the output files go **only** to `.scratch/eval_variants/` (gitignored, because children are in them).
3. **Invariance.** A re-encode at 480p with 32 kbps mono audio should leave the grade the same. We report the share of grades unchanged and the mean \|Δscore\|.
4. **Pairwise ranking.** From the 10 classes there are 45 pairs; 36 of them have different ordinal labels (the 9 same-label pairs are excluded). We report pairwise ordering accuracy on those 36 pairs (ties in the prediction count as half) plus Spearman. This has more statistical power than 10 point labels. It is still the same 10 classes, so we report it as supporting evidence only.
5. **Demos** stay out-of-distribution and are never used for tuning.

Cost control: variants run on a subset (about 8 variant files), with k=3 and no B1 transcription unless a test needs it.

### 7.2 Tier comparison: Flash vs Flash-Lite

Both tiers run on the same classes, with the same prompts, k and samples. We report:
- item-level agreement between the two tiers;
- each tier's item accuracy against the grader labels and against our silver labels;
- grade metrics;
- run variance;
- cost and latency.

**Rule:** Flash-Lite ships if it is within one exact hit of Flash on grades, and within 5 points on lenient item accuracy, at no worse run variance. Otherwise Flash ships.

Because of the API budget (see §7.4), the comparison starts on 1–2 non-holdout videos, and is widened only when the user says so.

### 7.3 Silver labels: how good is a careful reader of the transcript?

- Each of the 10 graded classes was annotated by an independent annotator who saw only the transcript and 7–11 frames. The label was hidden: inputs were renamed by class id only.
- Compared with the grader labels (35 rows), agreement was **strict 18/35 (51%) and lenient 32/35 (91%)**.
- Most of the disagreement was the annotators hedging toward `partial` (rapport strict 0.29, energy 0.40).
- So a realistic ceiling for strict item accuracy from transcript evidence is about 50–60%. Lenient accuracy, around 90%, is the meaningful item-level target.
- The repo copy, `eval/silver_items.csv`, keeps timestamps only. Quote-bearing evidence stays in `.scratch/silver/`, because quotes from confidential recordings are not committed.

### 7.4 Cost estimates and API budget (Flash tiers, paid-tier list prices checked 2026-10-22)

**Price per 1M tokens (input / output):**

| Model | Input | Output |
|---|---|---|
| gemini-3.8-flash | $0.75 | $3.75 |
| gemini-3.5-flash-lite | $0.30 | $2.50 |

gemini-3.8-flash prices double on 2027-01-01.

**Tokens per assessment (estimated, for a 50-minute class):**
- Main call: low resolution, about 100 tokens/s, so about 300k tokens.
- Two focus calls: high resolution, about 290 tokens/s × 600 s each, so about 350k tokens together.
- Total: about 650k input tokens per run. With k=3, about 2M input tokens plus about 50k output tokens.

**Cost per assessment:**
- Flash: about $1.6.
- Flash-Lite: about $0.7.

**Savings to apply before scaling:**
- Context caching of the uploaded video: cached input is billed at $0.075/M on Flash, so runs 2 and 3 of the k=3 cost about a tenth of run 1.
- Focus calls at medium resolution instead of high.

**API budget rules (from the user):**
- No Gemini calls without explicit permission.
- Evals and tests use at most 1–2 videos unless told otherwise.
- Everything else runs on the fake client and mocks.

One metadata-only call (listing the available model IDs) was made before this rule arrived. No media or transcript has been sent yet.

Caching: every Gemini response is stored in `.scratch/cache/` (gitignored), keyed by `sha256(media) | call | PROMPT_VERSION | model | sample_idx`. This makes re-scoring, LOO and ablations free and reproducible.

Mock mode: `MockObserver` serves fixture responses, including an "oracle-with-noise" generator built from the item ground truth. This lets the harness, scorer and app be tested end to end before `GEMINI_API_KEY` arrives. Mock numbers are never reported as results.

## 8. Privacy and ops

- Recordings and caches stay out of git.
- Uploaded files are deleted after each assessment, and app uploads are deleted after the report is built.
- Logs never contain transcript text or kid names.
- Gemini is approved by the user; organiser approval is still to be confirmed and is stated in the writeup.
- Hosting must handle uploads of up to about 350 MB and an async job of about 5–10 minutes with progress reporting.

## 8a. Explicit judgement calls (decided, recorded here)

1. **Mapping the grader's energy lines to labels.** "need more X" (for example "need more energetic and pleasant interaction") is labelled `partial`. "need a better/happier X" (for example "need a better happy interaction") is labelled `not_met`. The same rule is applied to all classes in `eval/ground_truth_items.csv`.
2. **Self-consistency temperature is 0.4, not 0.** At temperature 0 the k=3 runs come back almost identical, so their agreement would not be a meaningful confidence signal. The temperature is set in config, and the eval reports run variance so the choice can be revisited.

## 8b. B2 calibration caveat

The B2 thresholds were checked on only **n=3 local files**: speech-frame p90 of -17.4 dB for B482, against -15.0 and -15.9 dB for two other classes. Because of that:
- `audio_defect:low_level` is **informational only**. It adds a flag and an explanation line, and has no effect on the grade.
- The B2 `energy_warmth` vote is **off by default**, because the liveliness measure came out "met" on all 3 files.
- No threshold will be tuned on so few files. Both are revisited only through the eval harness, with more data.

## 9. Open items

- Organiser approval for Gemini.
- Choosing between Flash and Flash-Lite from the tier comparison (§7.2).
- Upload vs link input for graders.
- The hosting target.
