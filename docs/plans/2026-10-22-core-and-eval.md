# Core Assessor + Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Gemini-observer core with a deterministic scorer that grades a class recording Poor/Average/Good/Excellent with evidence, and an eval harness built alongside it.

**Architecture:**
- Gemini does the observing: it fills in a 7-item rubric checklist, using a main call over the full video plus focus calls on the opening and the closing. This runs k=3 times, and the results are majority-voted per item.
- Pure-Python code does the deciding. It validates evidence, applies hard gates, detects demos, and applies cutpoints to produce the grade.
- Modules (B1 transcript, B2 audio, XC crosscheck) plug in as extra voters or gate signals.
- The eval harness reuses cached observations, so LOO, ablation and pairwise analyses cost no new API calls.

**Tech Stack:** Python 3.12, uv, pydantic v2, google-genai, PyYAML, pytest, ffmpeg/ffprobe (system), Streamlit (later task).

**Spec:** `docs/design/system-design.md` (v4)

## Global Constraints

- Gemini is the only external provider; the env var is `GEMINI_API_KEY`. Use no Claude or Anthropic SDK anywhere in the app.
- The LLM never picks the grade. The grade comes only from `cqa.scorer.score`.
- Rubric weights are `individual_calling 20, rapport_intro 18, energy_warmth 18, camera_background 14, homework 12, closing 10, new_joiner_intro 8` (sum 100). Never fit them to labels.
- Status values are met=1.0, partial=0.5, not_met=0.0, not_applicable and not_observable = None.
- Prior cutpoints are `(50.0, 68.0, 84.0)`. LOO tuning stays within ±8 of each prior and is adopted only if it beats the priors by at least one exact hit.
- Coverage below 0.6 of the weight means abstain. Self-consistency uses k=3 and temperature 0.4.
- Recordings, caches and eval variants live only under `class recordings */` or `.scratch/`. Never commit them, and never put transcript text or kid names in logs.
- Holdout classes are `B552` and `B482`, scored only once, at the end.
- Grade-level accuracy rests on 10 real labels; reports must say so.

## Review Focus

1. Gemini timestamps such as `"75:30"` (minutes past 59) or `"1:15:30"` must both parse to seconds. Test: `test_parse_ts_variants` (Task 2).
2. If the model omits an item or returns an unknown item id, the item becomes `not_observable` and the scorer must not crash. Test: `test_missing_item_becomes_not_observable` (Task 3).
3. If the three runs disagree three ways (met/partial/not_met), the result is the median, `partial`, with agreement 1/3. Test: `test_three_way_tie_takes_median` (Task 3).
4. For a short recording (under 20 min), the focus windows must be clamped to [0, duration] and must not invert. Test: `test_focus_windows_clamped_short_video` (Task 6).
5. A demo or non-class session gets no grade even with a high score. Test: `test_demo_never_graded` (Task 4).

---

### Task 1: Scaffold, rubric and schemas

**Files:**
- Create: `pyproject.toml`, `cqa/__init__.py`, `cqa/rubric.py`, `cqa/schema.py`, `tests/test_rubric_schema.py`

**Interfaces (produces):**
- `cqa.rubric`:
  - `Status(str, Enum)` with members MET, PARTIAL, NOT_MET, NOT_APPLICABLE, NOT_OBSERVABLE (values `"met"`, …)
  - `STATUS_VALUE: dict[Status, float | None]`
  - `ORDINAL: dict[Status, int]` (not_met 0, partial 1, met 2)
  - `ItemDef(id, title, weight, description, anchors: dict[str, str])` (frozen dataclass)
  - `ITEMS: tuple[ItemDef, ...]`, `ITEM_IDS: tuple[str, ...]`, `WEIGHTS: dict[str, int]`
  - `Grade(IntEnum)`: POOR=0, AVERAGE=1, GOOD=2, EXCELLENT=3, with `.label` returning e.g. `"Excellent"`
  - `DEFAULT_CUTPOINTS = (50.0, 68.0, 84.0)`
- `cqa.schema` (pydantic):
  - `Evidence(t_start: str, t_end: str | None = None, modality: Literal["visual","audio","both"] = "both", observation: str, quote: str | None = None)`
  - `CameraObs(branded_background: Literal["present","absent","unclear"]="unclear", face_visible: Literal["mostly","sometimes","rarely","unclear"]="unclear", camera_on_while_teaching: <same literal as face_visible>="unclear", lighting: Literal["ok","dim","unclear"]="unclear")`
  - `ItemObservation(status: Status, evidence: list[Evidence] = [], rationale: str = "")`
  - `SessionType = Literal["regular_class","demo_trial","not_a_class","unknown"]`
  - `ObserverOutput(call: Literal["main","opening","closing"], window: tuple[float, float], items: dict[str, ItemObservation], camera: CameraObs | None = None, session_type: SessionType | None = None, class_start: str | None = None, class_end: str | None = None)`
  - `TranscriptTurn(start: float, end: float, role: Literal["teacher","kid","media","other"], text: str)`
  - `gemini_response_schema(call: str) -> dict`: a JSON schema the model must follow for that call. Items are restricted to the call's item list (opening: rapport_intro, new_joiner_intro, camera_background; closing: closing, homework, camera_background; main: all 7). `session_type`, `class_start` and `class_end` appear only for main.

- [ ] **Step 1: Write the failing tests.** `test_weights_sum_100` (`sum(WEIGHTS.values()) == 100`, plus the exact per-id values above); `test_status_values` (met 1.0, partial 0.5, not_met 0.0, N/A and not_observable None); `test_grade_labels`; `test_schema_roundtrip` (an `ObserverOutput` dumps to JSON and loads back equal); `test_gemini_schema_items_per_call` (the opening schema's item properties are exactly the 3 ids; main has `session_type`).
- [ ] **Step 2:** `uv run pytest tests/test_rubric_schema.py -q` → FAIL (import error).
- [ ] **Step 3: Implement.** `pyproject.toml` declares deps `pydantic>=2.7`, `google-genai`, `pyyaml`, `rapidfuzz`, `numpy`, and the dev dep `pytest`. Item descriptions and anchors come from spec §3, with the grader anchors paraphrased and no names.
- [ ] **Step 4:** pytest → PASS.
- [ ] **Step 5:** Commit `feat: rubric and observation schemas`.

### Task 2: Timestamps and evidence validation

**Files:** Create `cqa/evidence.py`, `tests/test_evidence.py`

**Interfaces:**
- `parse_ts(s: str) -> float`: accepts `"MM:SS"`, `"M:SS"`, `"HH:MM:SS"`, and `"75:30"` (= 4530 s). Raises `ValueError` on garbage.
- `fmt_ts(sec: float) -> str`: returns `"MM:SS"` below an hour and `"H:MM:SS"` otherwise.
- `validate(obs: ObserverOutput, duration: float, transcript: list[TranscriptTurn] | None = None) -> tuple[ObserverOutput, list[str]]`. It drops evidence outside `[0, duration]` or outside `obs.window` (±5 s tolerance). If a transcript is given and a quote has no turn within ±20 s with `rapidfuzz.fuzz.token_set_ratio ≥ 60`, the evidence is dropped. A met or partial item left with no evidence becomes `NOT_OBSERVABLE`, and the flag `unsupported_claim:<item>` is added.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_ts_variants`: `"05:07"`→307, `"75:30"`→4530, `"1:15:30"`→4530.
  - `test_fmt_ts`
  - `test_drops_out_of_range_evidence`
  - `test_drops_outside_window`
  - `test_quote_must_match_transcript`
  - `test_met_without_evidence_becomes_not_observable`: the flag is present.
  - `test_not_met_without_evidence_kept`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 3: Merge calls and self-consistency vote

**Files:** Create `cqa/aggregate.py`, `tests/test_aggregate.py`

**Interfaces:**
- `MergedRun(items: dict[str, ItemObservation], camera: CameraObs, session_type: SessionType | None, class_start: str | None, class_end: str | None)` (pydantic)
- `merge_calls(main: ObserverOutput, opening: ObserverOutput | None, closing: ObserverOutput | None) -> MergedRun`. It applies the spec §4.2 table:
  - homework takes the most positive status that has evidence;
  - camera fields take the most conservative value. Order for `branded_background`: absent < unclear < present. For face and camera: rarely < sometimes < mostly, with unclear ignored unless every value is unclear. For lighting: dim < ok.
  - Items missing from every call become `NOT_OBSERVABLE`; unknown ids are ignored.
- `ModuleVote(module: str, item: str, status: Status, weight: float = 1.0, evidence: list[Evidence] = [])`
- `ItemConsensus(status: Status, agreement: float, evidence: list[Evidence], votes: list[Status])`
- `Consensus(items: dict[str, ItemConsensus], camera: CameraObs, session_type: SessionType, session_agreement: float)`
- `vote(runs: list[MergedRun], extra: list[ModuleVote] = []) -> Consensus`. Voting is a weighted majority over statuses: each run counts 1, and each module vote counts its weight. A three-way tie takes the ordinal median. Null statuses (N/A, not_observable) win only when they hold the strict majority. Evidence is taken from the majority voters and de-duplicated within 10 s. Camera fields use the per-field mode across runs, falling back to the conservative value on a tie. `session_type` is the mode.

- [ ] **Step 1: Write the failing tests.**
  - `test_merge_opening_overrides_main_for_rapport`
  - `test_merge_homework_most_positive_with_evidence`
  - `test_merge_camera_conservative`
  - `test_missing_item_becomes_not_observable`
  - `test_vote_majority_and_agreement`: [met, met, partial] gives met with agreement 2/3.
  - `test_three_way_tie_takes_median`
  - `test_module_vote_breaks_tie`
  - `test_session_type_mode`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 4: Deterministic scorer

**Files:** Create `cqa/scorer.py`, `tests/test_scorer.py`

**Interfaces:**
- `GateSignal(module: str, field: Literal["branded_background","face_visible","camera_on_while_teaching","lighting"], value: str, n_frames: int)`
- `ScoringConfig(cutpoints: tuple[float, float, float] = DEFAULT_CUTPOINTS, coverage_min: float = 0.6, weights: dict[str, int] = WEIGHTS)`
- `Assessment(grade: Grade | None, score: float | None, session_type: str, confidence: Literal["high","medium","low"], flags: list[str], abstain_reason: str | None, items: dict[str, ItemConsensus], camera: CameraObs, coverage: float)`
- `grade_from_score(score: float, cutpoints) -> Grade`. Lower bounds are inclusive: 50→AVERAGE, 68→GOOD, 84→EXCELLENT.
- `score(c: Consensus, cfg: ScoringConfig = ScoringConfig(), gates: list[GateSignal] = []) -> Assessment`. Order:
  1. Session type: demo_trial gives grade None with session_type "demo". not_a_class or unknown, or `session_agreement < 2/3`, gives grade None with `abstain_reason="not_a_class"` or `"session_unclear"`.
  2. Coverage: below the minimum gives grade None with `abstain_reason="insufficient_observations"`; the score is still reported.
  3. Weighted score.
  4. Apply XC gate signals with at least 8 frames, conservative-only. Any disagreement with the consensus camera field flags `crosscheck_disagree:<field>`.
  5. Gates: branded_background absent caps at POOR with flag `compliance:no_branded_background`; face_visible rarely caps at POOR with flag `compliance:face_not_visible`; camera_on_while_teaching sometimes adds flag `improve:camera_off_at_times`.
  6. Confidence per spec §5.6.

- [ ] **Step 1: Write the failing tests.**
  - `test_all_met_is_excellent`
  - `test_grade_boundaries`: 49.9→POOR, 50→AVERAGE, 67.9→AVERAGE, 68→GOOD, 84→EXCELLENT.
  - `test_null_items_renormalised`: with new_joiner_intro N/A and the rest met, the score is 100.
  - `test_coverage_abstain`
  - `test_no_background_caps_poor`: all met plus background absent gives POOR with the flag.
  - `test_face_rarely_caps_poor`
  - `test_camera_sometimes_flag_only`
  - `test_demo_never_graded`
  - `test_session_unclear_abstains`
  - `test_xc_gate_conservative_only`: XC "absent" with 20 frames overrides a consensus of present and flags disagreement. XC "present" doesn't override a consensus of absent. XC with 5 frames is ignored.
  - `test_confidence_levels`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 5: Explanation from evidence

**Files:** Create `cqa/explain.py`, `tests/test_explain.py`

**Interfaces:**
- `build_explanation(a: Assessment) -> dict` with keys `headline: str`, `strengths: list[str]`, `improvements: list[str]`, `flags: list[str]`.
  - Each bullet looks like `"[MM:SS] <observation>"` and uses only validated evidence.
  - Met items go into strengths; partial and not_met go into improvements. A not_met item with no evidence uses its rationale, prefixed "Not observed:".
  - Headline: `"<Grade> (score 73/100, confidence medium)"`, or a demo/abstain message.

- [ ] **Step 1: Write the failing tests.**
  - `test_every_bullet_has_timestamp_or_not_observed`
  - `test_no_bullets_for_null_items`
  - `test_demo_headline`
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 6: Observer interface, mock, cache and media ingest

**Files:** Create `cqa/media.py`, `cqa/observer.py`, `cqa/cache.py`, `tests/test_observer.py`

**Interfaces:**
- `MediaRef(path: Path, sha256: str, duration: float)`; `probe(path: Path) -> MediaRef`. Uses ffprobe; the hash covers the first and last 8 MB plus the size, for speed.
- `CallSpec(name: Literal["main","opening","closing"], start: float, end: float, media_resolution: Literal["low","medium","high"])`
- `plan_calls(duration: float, focus_min: float = 10.0) -> list[CallSpec]`. The opening window is [0, min(600, d)] and the closing window is [max(0, d-600), d]. For d < 20 min, the windows are clamped and may overlap, but always satisfy start < end.
- `CallStats(input_tokens: int, output_tokens: int, latency_s: float, cost_usd: float)`
- `class Observer(Protocol)` with `observe(media: MediaRef, spec: CallSpec, sample_idx: int) -> tuple[ObserverOutput, CallStats]`
- `MockObserver(script: dict[str, list[dict]] | None = None, oracle: dict[str, Status] | None = None, noise: float = 0.0, seed: int = 0)`. In script mode it serves fixtures by `call` and `sample_idx`. In oracle mode it emits the given statuses, with evidence at the window start, flipping to a neighbour status with probability `noise`.
- `CachedObserver(inner: Observer, root: Path, model: str, prompt_version: str)`. The cache key is `sha256|call|start|end|res|prompt_version|model|sample_idx`, and each entry is stored as JSON, output plus stats.

- [ ] **Step 1: Write the failing tests.**
  - `test_focus_windows_clamped_short_video`: d=600 → both windows are [0, 600].
  - `test_plan_calls_long_video`
  - `test_mock_oracle_deterministic_with_seed`
  - `test_cache_hit_skips_inner`
  - `test_probe_reads_duration`: this test makes a 3 s clip with ffmpeg `lavfi` testsrc + sine into tmp_path. It contains no real recordings.
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 7: Pipeline

**Files:** Create `cqa/pipeline.py`, `tests/test_pipeline.py`

**Interfaces:**
- `PipelineConfig(k: int = 3, sample_offset: int = 0, scoring: ScoringConfig = ScoringConfig(), modules: dict[str, bool] = {"b1": False, "b2": False, "xc": False})`
- `AssessmentRun(assessment: Assessment, runs: list[MergedRun], stats: list[CallStats], flags: list[str])`
- `assess(media: MediaRef, observer: Observer, cfg: PipelineConfig, transcript: list[TranscriptTurn] | None = None, module_votes: list[ModuleVote] = [], gates: list[GateSignal] = []) -> AssessmentRun`. Flow: plan_calls → for i in `range(offset, offset+k)`: observe each spec → validate → merge_calls → then vote over all runs → score.

- [ ] **Step 1: Write the failing tests.**
  - `test_pipeline_oracle_excellent`: an all-met oracle gives EXCELLENT with 9 calls.
  - `test_pipeline_unsupported_claim_flag_propagates`
  - `test_pipeline_sample_offset_uses_distinct_samples`
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 8: Eval data files and metrics

**Files:** Create `eval/__init__.py`, `eval/dataset.yaml`, `eval/ground_truth_items.csv`, `eval/metrics.py`, `tests/test_metrics.py`

**Interfaces:**
- `dataset.yaml` holds, per class: `id, folder, video_glob, vtt_glob, label (excellent|good|average|weak|quality_issue|demo), ordinal (0..3 | null), holdout: bool`. The holdouts are B552 and B482. The ordinal mapping is weak and quality_issue → 0, average → 1, good → 2, excellent → 3, demo → null.
- `ground_truth_items.csv` has columns `class,item,status,source,evidence`, with `source=grader`. It is transcribed from feedbacks.pdf for the 7 items. Statuses are `met|partial|not_met`, and blank rows are omitted.
- `eval.metrics`:
  - `exact_acc(y, p)`, `within1_acc(y, p)`, `mae(y, p)`, `spearman(y, s)`
  - `pairwise_acc(y, s) -> tuple[float, int]`: over pairs with y_i ≠ y_j, a correctly ordered pair scores 1 and a tied score scores 0.5; returns the accuracy and the pair count.
  - `item_accuracy(gt_rows, pred: dict[str, dict[str, Status]], lenient: bool) -> dict[str, tuple[float, int]]`
  - `majority_baseline(y) -> list[int]`

- [ ] **Step 1: Write the failing tests.**
  - `test_metrics_known_values`
  - `test_pairwise_counts_36_for_label_distribution`: with labels [3,3,2,2,1,1,0,0,0,0], n=36.
  - `test_item_accuracy_lenient`
  - `test_dataset_yaml_valid`: 12 entries, 2 holdouts, 2 demos with null ordinal.
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 9: LOO cutpoints

**Files:** Create `eval/loo.py`, `tests/test_loo.py`

**Interfaces:**
- `tune_cutpoints(scores: list[float], y: list[int], prior=DEFAULT_CUTPOINTS, window=8.0, step=1.0) -> tuple[float, float, float]`. This is a grid search within ±window, keeping cutpoints monotonic, that minimises MAE. Ties go to the candidate closest to the prior (L1 distance).
- `loo_eval(scores, y, prior) -> dict` with keys `prior_preds`, `loo_preds`, `prior_exact`, `loo_exact`, `adopt_tuned: bool` (true iff loo_exact ≥ prior_exact + 1).

- [ ] **Step 1: Write the failing tests.**
  - `test_tune_respects_window`
  - `test_tune_prefers_prior_on_ties`
  - `test_loo_adopt_rule`
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 10: Gemini observer (real) and prompts

**Files:** Create `cqa/prompts.py`, `cqa/gemini_observer.py`, `tests/test_gemini_observer.py`

**Interfaces:**
- `PROMPT_VERSION = "v1"`; `build_prompt(spec: CallSpec, duration: float) -> str`. The prompt contains the domain primer, the item anchors for the call's items, the timestamp and quote rules, and the instruction "never name or describe children".
- `GeminiObserver(client: genai.Client, model: str, temperature: float = 0.4, prices: dict[str, float])` implements `Observer`.
  - `uploaded(media)` is a context manager that uploads once, polls until ACTIVE, yields the file, and calls `files.delete` in `finally`.
  - `observe` calls `models.generate_content` with `types.Part(file_data=..., video_metadata=VideoMetadata(start_offset=f"{s}s", end_offset=f"{e}s", fps=1))` and `GenerateContentConfig(temperature, response_mime_type="application/json", response_schema=gemini_response_schema(spec.name), media_resolution=MEDIA_RESOLUTION_LOW|HIGH)`.
  - It parses the response into an `ObserverOutput`, with `window=(s, e)`, and builds `CallStats` from `usage_metadata`.
  - On a parse error it retries once, then returns an output whose items are all not_observable and flags it.
- The model id comes from env `CQA_GEMINI_MODEL`. Prices come from `cqa/pricing.yaml`; the values are filled in when the model is pinned.

- [ ] **Step 1: Write the failing tests.** Use a fake client object, not the network.
  - `test_upload_deleted_on_error`
  - `test_request_has_schema_offsets_and_resolution`
  - `test_parse_failure_retries_then_not_observable`
  - `test_stats_from_usage_metadata`
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.
- [ ] **Step 6 (once the key exists):** run a live smoke test on a single 2-minute ffmpeg excerpt of one class, stored in `.scratch/`. Check the schema parses and the file gets deleted.

### Task 11: Eval runner, reports and ablation

**Files:** Create `eval/run_eval.py`, `tests/test_run_eval.py`

**Interfaces:**
- CLI: `uv run python -m eval.run_eval --observer mock|gemini --samples 6 --k 3 --include-holdout/--no-include-holdout --modules b1,xc --out .scratch/eval_runs/<ts>/`
- Writes `results.json` and `report.md`, containing:
  - a grade table and confusion matrix;
  - exact, within-1, MAE, Spearman and pairwise numbers, alongside the majority baseline;
  - LOO prior vs tuned;
  - run variance: assessments A and B use samples 0–2 and 3–5; report flip rate and score SD;
  - item accuracy against the grader ground truth and the silver labels, reported separately;
  - cost and latency: p50/max seconds and $ per class;
  - an ablation table over {core, +b1, +b2, +xc, all};
  - an OOD demos section;
  - the banner "Grade-level accuracy rests on 10 real labels."
- `ablate(cached_runs, module_votes, gates, configs) -> list[dict]`: rescoring only, no observer calls.

- [ ] **Step 1: Write the failing tests.**
  - `test_runner_mock_end_to_end`: a mock oracle built from the ground truth produces a report.md containing every section header and the banner.
  - `test_ablation_no_observer_calls`: the mock's call counter doesn't change.
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task 12: Silver item labels

**Files:** Create `eval/silver_items.csv` (same columns as the grader ground truth, `source=ours`, evidence `MM:SS "quote"` with kid names replaced by `[kid]`).
- [ ] **Step 1:** For each of the 10 graded classes, annotate all 7 items from the .vtt, recording status, timestamp and quote. Do this before looking at any model output for that class. One subagent per class is fine.
- [ ] **Step 2:** `test_silver_complete`: 70 rows (10 classes × 7 items), with an `N/A` status allowed for new_joiner_intro.
- [ ] **Step 3:** Commit. The quotes must not contain kid names; a grep test checks this against the roster of speaker labels.

### Task 13: Metamorphic, counterfactual and invariance variants

**Files:** Create `eval/variants.py`, `eval/variants.yaml`, `tests/test_variants.py`

**Interfaces:**
- `variants.yaml` is a list of entries `{id, source_class, op: trim_opening|cut_segment|blackout_tile|cut_tail|pad_tail|reencode_low, params, expect: {item?: str, direction: down|flip_to_not_met|gate_fires|unchanged, tol?: float}}`.
- `make_variant(src: Path, op: str, params: dict, out_dir: Path) -> Path` builds an ffmpeg command. Output paths must sit under `.scratch/eval_variants/`; anything else raises `ValueError`.
- `check_expectation(base: Assessment, var: Assessment, expect: dict) -> bool`
- The initial set:
  - trim_opening on B472 and B491 (cut to class_start + 6 min);
  - cut_segment on B472 at 45:00–47:40;
  - blackout_tile on B552 (box at the top-right tile; the region is measured from frames in Task 13 Step 1);
  - cut_tail on B441 (from the teacher's last speech + 30 s);
  - pad_tail on B491 (+5 min black and silent);
  - reencode_low on B472 and B435a (480p, 32 kbps mono).
- [ ] **Step 1:** Measure the tile box on frames already in `.scratch/frames/`.
- [ ] **Step 2: Write the failing tests.**
  - `test_variant_path_guard`
  - `test_ffmpeg_cmd_for_each_op`: a dry run that only builds the command.
  - `test_check_expectation_directions`
  - `test_make_variant_on_synthetic_clip`
- [ ] **Steps 3–5:** implement, run → PASS, commit.
- [ ] **Step 6 (with key):** run the variants through the pipeline, then add a variants section (direction accuracy and |Δscore|) to report.md.

### Later tasks (separate plan once the core eval runs)

- **B1:** Gemini audio transcription (chunked, with VAD snap), pseudonymisation, and the text judge, producing ModuleVotes.
- **B2:** the audio liveliness vote.
- **XC:** local frame gate signals.
- **App:** Streamlit, with an async job queue, progress reporting and file deletion.
- **Hosting.**
- **README and writeup.**
