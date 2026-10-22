# Class Quality Assessor: System Design

Status: draft, not yet built. Part 1 is the merged design. Where Part 1 disagrees with the component docs in Part 2, Part 1 wins. Part 2 holds the component designs as they were written, each by a separate agent.

Revision 2: the user approved sending the video to Gemini (paid tier, uploads deleted after use). Path A is now Gemini multimodal over the full video and audio. The local face and logo checks stay on as cheap cross-check gates. The architecture choice will be decided by a measured bake-off (§6a). The local-only Path A design in Part 2 is superseded; its factors and gates are reused by §4A below.

## 1. What we are building

The app takes a recording of an 8x8 online chess class for kids (an upload or a link) and returns a grade: Poor, Average, Good or Excellent. Alongside the grade it gives a confidence level, any compliance flags, and an explanation in which every claim cites a timestamp and a piece of evidence.

The grader's feedback lines (feedbacks.pdf) are the rubric. They cover five themes:
- rapport, the intro and the closing;
- including every kid individually;
- the branded 8x8 background, camera on, face visible, and lighting;
- homework;
- energetic, pleasant delivery.

The rubric says nothing about whether the chess content is correct.

## 2. Architecture

```
upload/link
  -> Stage 0  ingest (local): ffprobe, extract 16 kHz mono audio, sample frames, check session type
  -> Stage T  transcript: use the uploaded .vtt if one is provided; otherwise Gemini transcribes the audio
              (chunked, JSON, timestamps, role labels teacher/kid/media/other); then local pseudonymisation (§4T)
  -> Stage 1  shared timeline (local): transcript turns, plus the screen state from path A, plus voice-activity detection.
              Each segment is labelled: pre-start | opening | teaching | silent play | played video | closing | post-class tail
  -> Path A   Gemini multimodal on full video+audio (low media res, ~1 fps, temp 0, strict JSON)
              -> A score 0-100, factor scores, evidence with timestamps (snapped to the Stage-1 timeline)
     A-local  local face/logo/camera/lighting checks on sampled frames -> cross-check gates
              (if A-local and Gemini disagree, raise a flag)
  -> Path B1  transcript only     -> B1 score 0-100, factor scores, quotes with timestamps
  -> Path B2  raw audio, LOCAL    -> B2 score 0-100, defect flags, energy evidence
  -> Fusion   gates -> weighted score -> cutpoints -> grade + confidence
  -> Explainer (Gemini text, evidence IDs only) -> report JSON -> UI   [no Claude anywhere]
```

Stage 1 was added after weighing the component designs; none of them proposed it as a stage. Every path was independently tripped up by the same misleading signals: silent arena play counted as dead air, lesson-video narration credited to the teacher, and the tail recorded after class ends. So segmentation happens once and all three paths read from it. Path A provides the screen state (arena or tournament page, video player, black screen). The transcript provides speech. Played-video segments are found from the "Audio shared by" labels plus screen-share detection of a video player, and are excluded from every teacher metric.

## 3. Council resolutions

Where the component designs conflicted, this is what we settled on.

| Conflict | Resolution |
|---|---|
| Transcription source (revision 3) | **AssemblyAI is dropped.** If a .vtt comes with the upload, we use it. Otherwise Gemini transcribes the audio (§4T). Gemini is the only external provider, and it already receives the full video. Raw audio containing kids' voices still goes to Gemini; the user has approved this, and the organiser's approval is still open (§9). |
| Path A "multimodal" vs "kids' faces never leave the box" | **Revision 2: the user approved Gemini (paid tier) for the full video.** On the paid tier, data is not used for training (to verify at build). Each uploaded file is deleted explicitly with `files.delete` as soon as the call returns; the File API's automatic expiry is only a backstop. The local checks (YuNet/MediaPipe face detection, logo template match, camera-on, luma) are kept as cross-checks. The organiser's permission is still an open question (§9 Q1). |
| Weight of B2 (fusion doc said 0.15, B2 doc said 10-15%, devil's advocate said gate only) | **B2 weight is 0.10 plus a defect gate.** Probes show that Zoom noise-gates the audio, so noise, hum and SNR carry no information. Only the speech level of B482 (-23.4 dB at p90, against -13.5 to -18.6 for every other class) and the teacher's vocal liveliness carry any signal. |
| B1 factor weights (two different lists) | Use the list from the B1 doc (below). Its weights total 100 and include tech-issue handling. |
| Face gate vs a teacher tile that can't be found | The gate fires only when the teacher tile is found in at least 8 sampled frames and the face is absent from more than 50% of them. If the tile is never found, the result is `unverified` (lower confidence), not Poor. |
| Path weights | **B1 0.60, A 0.30, B2 0.10.** These are fixed priors and are never fitted to the labels. |
| Using the LLM for the final grade | The grade is decided by deterministic fusion. The LLM writes the explanation only, and every bullet must cite evidence IDs that a code validator checks. |

## 4. Paths at a glance

### A: Gemini multimodal (full video + audio)

**Call setup**
- The whole recording goes up through the File API.
- `media_resolution` is low (about 66 tokens per frame), sampling is about 1 fps, and audio costs about 32 tokens per second. That comes to roughly 100 tokens per second: about 360k input tokens for a 60-minute class and about 540k for 90 minutes. Both fit a 1M-token context window, so there is one call per class.
- We use the current Gemini Pro-tier model; the exact model ID gets pinned at build time. Flash is a fallback if Pro is too slow or costly, and the bake-off measures both.
- Temperature is 0. The response must follow a JSON schema (`response_schema`).
- If Pro turns out too slow, clipping with `start_offset`/`end_offset` lets us run per-segment calls in parallel.

**The prompt includes**
- The rubric anchors for each factor.
- The Stage-1 segment table (start and end time of each segment, and its type), so Gemini knows which stretches are silent play or played video.
- An instruction to cite `MM:SS` timestamps and describe what it saw or heard.
- An instruction never to name or describe kids.

**Output**
- For each factor: a score from 0 to 4, or null if it isn't observable.
- 1 to 3 pieces of evidence per factor, each as `{t: "MM:SS", segment_id, modality: visual|audio|both, observation}`.
- A session-type guess.

**Grounding rules, enforced in code**
- If an evidence timestamp lands outside the recording, or inside a segment whose type contradicts the claim, the evidence is dropped. An example of a contradiction: "teacher explaining" timestamped in a silent-play or post-class segment.
- If any quoted speech appears, it is fuzzy-matched against the Stage-T transcript within ±15 s of the timestamp; quotes that don't match are dropped.
- If a factor is left with no evidence, it becomes null.

| Factor | Weight | Grader anchor |
|---|---|---|
| Branded 8x8 background present | 15 | B392 had none |
| Teacher face visible and camera on while teaching | 15 | B441 face never visible; B482 camera off at times |
| Tone and energy (vocal liveliness, enthusiasm) | 15 | "needs more energetic communication" (B435a, B558) |
| Warmth and pleasantness (smiling, friendly voice, laughter, kids' reactions) | 15 | "more pleasant/happy interaction" (B552, B435a, B558, B392, B441) |
| Engagement with kids seen and heard (kids' cameras, responsiveness, turn-taking feel) | 10 | "considers everyone" (B472, B435g) |
| Lighting and video clarity (including virtual-background cutout noise) | 10 | B435a light; B516 clarity issues |
| Rapport opening and closing as seen and heard | 10 | B482/B441 "directly entered" |
| Use of interactive content (puzzles, videos, arena) | 5 | B491 suggestion to play topic videos |
| Class bounds, dead air and black screen (teaching segments only) | 5 | B482 start, B441 tail |

**A-local cross-check gates.** These run on about 120 sampled frames, locally, and take about 30 seconds of CPU.
- They detect the branded logo in the teacher tile, the teacher's face, camera on or off, and luma (brightness).
- The teacher is identified from the name label (local OCR) plus the main diarized speaker, never from a face. "Tile absent" is its own state.
- If Gemini and A-local disagree on the branded background or on face visible (one says present for at least 50% of teaching time, the other says absent): set flag `crosscheck_disagree:<factor>`, drop confidence one notch, and let the compliance gate use the more conservative verdict only when A-local's frame count is at least 8.

### T: transcript source (Stage T)

**1. Source priority**
- If a .vtt (or .srt) is uploaded alongside the video, parse it. That is Zoom's own transcript, with display-name speakers.
- Otherwise, Gemini transcribes the audio.
- Graders will most likely upload only the mp4, so **Gemini transcription is the default path, and the bake-off must measure it** (see §6a).

**2. Gemini transcription**
- **Input.** Extract the audio locally to 16 kHz mono (about 1 MB per minute in Opus or AAC) and upload it once through the File API. Audio-only upload is far smaller and faster than video.
- **Chunks.** Split into 10-minute chunks with 30 seconds of overlap, using `start_offset` and `end_offset` on the same uploaded file. That is about 6–9 chunks per class, run in parallel.
  - Why chunk: one 60–90 minute class produces roughly 12k words, about 20–40k output tokens once JSON keys and timestamps are added. That is close to the model's output-token cap (to be checked for the chosen model). Timestamp drift also grows with length.
  - 10-minute chunks keep each response around 3–6k tokens and limit drift.
- **Output schema per chunk.** A list of turns `{start: "MM:SS", end, role: teacher|kid|media|other, speaker_hint: "kid_a"|..., text, lang}`.
  - `media` marks narration from a played lesson video, which removes the problem of that narration being credited to the teacher.
  - Temperature is 0.
- **Diarization.** We ask for roles, not voice identities.
  - Teacher vs kid is a much easier call than "speaker 3 vs speaker 5", and roles are what the rubric needs.
  - Distinct kids are counted later from spoken names (after pseudonymisation) and from `speaker_hint` within a chunk. Hints are not trusted across chunks.
  - Each chunk's prompt carries a one-line description of the teacher from chunk 1 (for example "female adult, Indian-English accent, leads the lesson") to keep roles consistent.
- **Drift control.**
  - Chunk-relative timestamps are converted to absolute time using the chunk offset.
  - Each turn's start is snapped to the nearest local voice-activity onset within ±3 s. Turns that can't be snapped are flagged.
  - Overlaps between chunks are de-duplicated by matching text in the 30-second overlap window.
  - A per-class `drift_score` (the share of turns snapped) is reported, and a low score lowers confidence.
- **Failure handling.**
  - A chunk whose JSON is invalid or truncated is retried once, then split in half.
  - A chunk where more than 30% of the audio has no speech is accepted as silent play, not treated as a failure.
  - Non-English and code-mixed speech is kept with a `lang` tag. If more than 30% of the class is non-English, B1 goes to low confidence or abstains.

**3. Pseudonymisation (local, after transcription)**
- Names are replaced with stable pseudonyms: Kid1..KidN, Teacher, Parent.
- Names are found from Gemini's `speaker_hint` labels, from vocatives (forms of address such as "[Name], your move"), and from local named-entity recognition.
- If a .vtt is the source, its display names are added to the name list.
- There is a fail-closed leak grep before the scoring call.
- Even though the provider is the same, this keeps the scorer prompt clean and the logs name-free. It also keeps "calls each kid individually" measurable through the pseudonyms.

### B1: transcript (Gemini on text only, names replaced with pseudonyms)

| Factor | Weight |
|---|---|
| Individual inclusion and how evenly kids are included | 20 |
| Rapport and intro | 15 |
| Energy, praise and warmth | 15 |
| Questioning quality | 12 |
| Conclusion | 10 |
| Homework (searched across the whole class, not just the closing window) | 10 |
| New joiners get to self-introduce | 8 |
| Clarity | 7 |
| Tech-issue handling | 3 |

- Each factor is scored 0-4 against anchored descriptions. A factor that can't be observed is null, and the weights are renormalized.
- Some factors are computed deterministically: time to the teacher's first speech, kid turns per minute during teaching segments, number of distinct kids addressed, and a Gini measure of how evenly kids participate. The LLM judges the rest.
- Kid names are replaced with stable pseudonyms (Kid1..KidN) using a local roster plus local named-entity recognition. The redaction fails closed, and a grep checks for leaked names before any API call. We use pseudonyms rather than blanket `[NAME]` redaction, which would make "calls each kid individually" impossible to measure. See §4T for how names are found.
- The judge is one Gemini Pro-tier call per class, text only, on the pseudonymised transcript of about 20k tokens. Temperature is 0 and the output follows a strict JSON schema. The rubric prefix is cached (context caching). No Claude is used anywhere in the system. Estimated cost is about $0.03–0.06 per class; this price has not been checked and the bake-off will measure it.
- Every quote must match the transcript verbatim, with a timestamp. Quotes that don't match are dropped, and the factor becomes null.

### B2: audio signal (local)

| Factor | Weight |
|---|---|
| Teacher vocal liveliness (pitch range, loudness dynamics) | 30 |
| Teacher level and consistency | 20 |
| Intelligibility (DNSMOS) | 15 |
| Dead air during teaching segments only | 15 |
| Speaking rate | 10 |
| Overlapping speech | 5 |
| Defects | 5 |

- Tools: ffmpeg, numpy, pyworld or pYIN, and DNSMOS ONNX, with silero-vad as a fallback. This adds less than 3 minutes of CPU per class.
- B2 is mainly a defect detector and a source of evidence about vocal energy.

## 5. Fusion, thresholds, calibration

1. **Session type first.** A demo (1:1 lesson, parent present, onboarding or enrolment talk) gets `grade: null`, an observations-only report, and a notice that it is not a regular class. The app abstains entirely when:
   - the speech recognition is garbage (under 40 words per minute, or more than 30% unintelligible or non-English);
   - the recording is shorter than 15 minutes;
   - more than 40% of it is black screen.
2. **Gates** can only lower a grade. Missing branded background, or a face that is verifiably absent, caps the grade at Poor and adds a `compliance` flag. This matches the quality-issue folder. The gates must not fire on Weak B558 or B482, both of which kept the background.
3. **Score.** Fused = 0.60·B1 + 0.30·A + 0.10·B2. If a path is missing, the remaining weights are renormalized and confidence drops one notch. If B1 is missing, the app abstains.
4. **Cutpoints** are set as priors before any labelled class is run: Poor below 50, Average 50-67, Good 68-83, Excellent 84 and up. Leave-one-out tuning may move only these three numbers, each by at most ±8. We ship the tuned values only if they beat the priors by at least one exact match.
5. **Confidence** is high, medium or low. It drops a notch if a path is missing, if the score is within 4 points of a cutpoint, if the paths disagree by more than 30 points, or if transcript quality is marginal (low `drift_score`, failed chunks, or a high non-English share).

## 6. Validation (honest version)

1. Freeze the prompts, factor definitions and weights. Write down the expected result for each class first: `expectations.csv`, derived from the grader lines.
2. Run the 10 graded classes (weak and quality-issue both map to Poor). Report:
   - exact accuracy;
   - within-one accuracy;
   - mean absolute error (MAE) on a 0-3 scale;
   - Spearman correlation;
   - the confusion matrix;
   - a majority-class baseline for comparison;
   - run-to-run variance from 3 repeated runs.
3. Per-factor checks against the grader lines: rapport, homework, inclusion, background and face, as in the laya experiment. Expect thin ground truth; the laya experiment showed a majority-class baseline can tie a model.
4. Run the two demos as out-of-distribution tests. The English demo should return `demo`; the Malayalam demo should abstain.
5. Targets: at least 6/10 exact, 10/10 within one, MAE of 0.5 or less, and both quality-issue classes graded Poor with a flag.
6. Change the design if:
   - Spearman is below 0.5;
   - A or B2 is nearly constant across classes (demote it to a gate);
   - Good and Average can't be separated (report "Average–Good" at low confidence).
7. To limit circularity (the rubric is in the prompt and the grader wrote the labels), keep 2 classes out of all prompt iteration.

## 6a. Bake-off: choose the architecture with data

All experiments use the same 10 graded classes (weak and quality-issue map to Poor), the same holdout rules (2 classes are kept out of all prompt iteration and scored only once, at the end), and expectations frozen before the first run. Prompts, schemas and weights are frozen per experiment before scoring.

| ID | System | Calls per class |
|---|---|---|
| B0 | Baseline: always predict the majority class | 0 |
| E1 | Gemini end-to-end only: one call on the full video, rubric in the prompt, returns grade + evidence | 1 Gemini |
| E2 | B1 only: Gemini transcript, then pseudonymised transcript, then Gemini text-only rubric judge, then cutpoints | ~6–9 transcription chunks + 1 Gemini (text) |
| E2-vtt | Same as E2, but on the Zoom .vtt instead of a Gemini transcript. Diagnostic only, not eligible to win | 1 Gemini (text) |
| E3 | E1 + E2 fused (0.5/0.5 on 0-100 scores, then the same cutpoints) | E1 + E2 |
| E4 | Full system: A (Gemini) + A-local gates + B1 + B2 + fusion (§5) | all |

**Metrics, per experiment**
- **Accuracy:** exact accuracy, within-1 accuracy, mean absolute error (MAE) on a 0-3 scale, and Spearman correlation against the ordinal label.
- **Separate checks:** quality-issue classes caught (Poor plus a flag), and whether both demos return demo/abstain.
- **Stability:** 3 runs per class. Report the share of classes whose grade changes across runs, and the standard deviation of the 0-100 score.
- **Cost:** measured from the token counts each API reports, in $ per class.
- **Test conditions:** E2, E3 and E4 run on **Gemini transcripts**, not the Zoom .vtt files, because graders will most likely upload only an mp4. E2-vtt is reported next to E2. The accuracy and MAE gap between them shows how much the transcript source costs, which is a useful finding either way.
- **Transcript quality vs the .vtt:**
  - Turn-level word overlap.
  - Teacher/kid role agreement, using .vtt speakers mapped to roles.
  - Median absolute start-time offset between matched turns.
  - Count of distinct kids found.
  - Share of played-video narration correctly tagged `media` (for example, about 35 minutes of narration in B558).
- **Latency:** p50 and max wall-clock time per class, end to end.
- **Explanation groundedness:** the share of evidence items that survive validation (timestamp fits the timeline, quote matches). Plus 1 blind spot-check per class against the grader's line: does the explanation mention the grader's main point?

**Decision rule, written before any run**
1. Rank by within-1 accuracy, then exact accuracy, then MAE.
2. A more complex system wins only if it beats the simpler one by at least 1 exact hit, or by at least 0.2 MAE, with no worse stability.
3. Otherwise the simpler, cheaper system wins.
4. Any system that grade-flips on more than 2 of 10 classes across 3 runs is disqualified.
5. If nothing beats B0 on exact accuracy, we say so in the writeup and ship the most grounded explainer, with low-confidence output.
6. Must-have whatever the winner: the quality-issue classes get flagged. If the winner lacks gates, the A-local gates are bolted on.

**Budget.** About 10 classes × 3 runs × (E1 + E2 + E4, with E3 reusing their outputs). The figures below are rough guesses from token counts and need checking against current prices before we commit:
- Gemini: about 500k tokens per call, so about 60 Pro calls in total.
- Gemini text (B1): about 20k tokens per call, so about 30 calls (E2 and E4 share them).
- Gemini transcription: about 6–9 audio chunks per class (audio is about 32 tokens per second, so about 115–175k input tokens per class). Run once per class and cached; the cache is shared by E2, E3 and E4. Transcription non-determinism is checked separately by transcribing 2 classes twice.

The bake-off results table goes into the README and the 300-word writeup; it is the "problem-solving with data" story.

## 7. Cost, latency, hosting

- **Per class:** Gemini transcription of about 115–175k audio input tokens plus about 20–40k output tokens (Flash-tier is likely sufficient; the bake-off will compare). B1 judging about $0.03–0.06 (Gemini, text only), plus one Gemini multimodal call on about 360–540k input tokens (Pro pricing above 200k tokens is higher; to be measured in the bake-off). Everything else is local. None of these prices has been checked.
- **Gemini upload:** the full mp4 goes up through the File API (up to 20 GB on the paid tier). Uploading a 340 MB file plus processing adds minutes. We delete the file immediately after the call returns.
- **Wall-clock time:** about 8–15 minutes for a 60-minute class. We need:
  - an async job queue;
  - per-stage progress in the UI;
  - a result URL that survives a page refresh;
  - a precomputed sample report for impatient graders.
- **Upload size:** Streamlit Cloud's default limit is 200 MB, below our 340 MB file. Options:
  - accept Drive links and fetch the file server-side;
  - raise the limit on a self-managed host or Hugging Face Space;
  - document an audio-only or compressed upload path.
- **Retention:** delete the uploaded video, audio and transcript once the job finishes, and never write transcript text to logs.

## 8. Build order (MVP first, from the devil's advocate)

1. Stage 0/T/1: ingest, Gemini transcription with chunking and drift snapping, pseudonymisation, and the timeline. Cache everything.
2. Build E1 (Gemini end-to-end) and E2 (B1) as harnesses, then run the bake-off for B0, E1, E2 and E3.
3. A-local gates, B2, fusion, then E4 in the bake-off.
4. Pick the winner using the §6a rule. Then build the report UI and app hardening (async jobs, size limits, link input, deletion).

Cut entirely: local noise and SNR factors, and laya.

## 9. Open questions (only the user or organiser can answer)

1. The user has approved Gemini (full video, raw audio, and the pseudonymised transcript); it is the only external provider. Has the organiser approved it, given that the brief says "services specifically permitted by the organiser"? The writeup should state what was sent where.
0. API keys: only `GEMINI_API_KEY` is needed. It is not currently set in the environment, and there is no .env file in the repo.
2. Is a hosted app that processes graders' uploads on a remote server acceptable at all? Which host, and what retention?
3. Will graders upload a file (sizes up to 340 MB) or paste a link (Drive or YouTube)?
4. For demo sessions, should the app abstain or give an observations-only report?
5. Is there a reference 8x8 logo or background image for template matching?
6. Are there more labelled classes, or grader feedback beyond feedbacks.pdf?
7. Is "Average–Good, low confidence" an acceptable output, or must the app always pick one grade?
8. Do you approve freezing the weights above before validation?

---

# Part 2: component designs (as written by each agent)

<a id="path_A"></a>
> **Superseded by Part 1 §4A (revision 2: Gemini multimodal).** Its local checks are reused as the A-local cross-check gates.
## Path A: video + audio (on-screen conduct + class feel)

### Purpose
Score the *teacher-presentation/compliance* half of the rubric that transcripts cannot see: camera on, face visible, branded 8x8 background, lighting, cutout quality, interactive content, class boundaries. Output a 0-100 score + timestamped evidence for fusion. Audio-only judgements (tone, energy) are owned by B2; transcript judgements (rapport, homework, individual calls) by B1. Path A consumes B2's speech/silence segmentation only to label "teaching vs. silent-play" time.

### Probe findings (local frames)
- Frames are 1280x680 (downscaled). Sampled frames show two modes: screen-share (chess site) and full-frame speaker view (e.g. B491 p2: a kid, portrait video pillarboxed, name label bottom-left). In B441 p30 (ChessKid lesson) no speaker tile is visible at all, so "tile absent" is a normal state and must be its own class, not "camera off".
- Therefore the tile can be anywhere/absent; detect it, don't hard-code coordinates (use a top-right prior only as a search hint).
- Identity risk: tile/full-frame shows whoever speaks, often a kid. The teacher's tile must be identified by the Zoom name label (OCR of the label, matched to the teacher = the diarized speaker who talks most / "Audio shared by" owner), never by face.

### Privacy: three options
1. **Fully local (RECOMMENDED).** All pixels stay on the host. Tools: ffmpeg frame sampling; layout classifier (screen-share / speaker-view / black / tile-absent) via simple geometry + edge heuristics; local face detector (OpenCV YuNet or MediaPipe; BlazeFace); logo/background template match (OpenCV multi-scale on a crop of the 8x8 logo, plus colour-histogram of the tile border); Laplacian/brightness for lighting; segmentation-edge noise via frame-diff flicker on tile boundary. Only derived numbers + timestamps leave (to the Claude judge/fusion). No permission needed beyond hosting the app yourself. Cost: CPU only. Trade-off: less "semantic" richness; thresholds need calibration.
2. **Local blur + hosted vision LLM.** Detect tile, blur/drop every face except the identified teacher's, send tiles to Claude vision. Fails the rule: teacher face is still biometric, kids appear in the tile when speaking, blur detection misses are catastrophic, and "organiser-permitted services" is unconfirmed. Do not do without written organiser OK.
3. **Hosted multimodal video model (e.g. Gemini video).** Best semantic coverage, simplest code, but uploads kid faces/voices to a third party. Only with explicit organiser permission and a data-processing agreement. Present as a documented future option, not built.

**Recommendation: Option 1.** Multimodal is realised as local vision features fused with audio segmentation (B2) and transcript (B1) -- "multimodal at the feature level". Needs permission from nobody; ask the organiser only if they want Option 2/3. State in README that raw frames never leave the process and temp files are deleted after analysis.

### Factors (weights sum to 100)
| Factor | Wt | Measured as | Rubric tie |
|---|---|---|---|
| Branded 8x8 background present (% of teacher-tile frames) | 25 | logo/template + palette match inside teacher tile | B392 "no background"; all others "kept background" |
| Teacher face visible (% of teacher-on-camera frames, face >= min size, frontal) | 20 | local face detector on teacher tile | B441 "face not visible" |
| Teacher camera-on % during teaching segments | 15 | teacher tile present when teacher speaks (join with diarization) | B482 "camera sometimes off" |
| Lighting/exposure | 10 | tile luma mean, clipped-shadow %, face-vs-bg contrast | B435a "sit where there is light" |
| Virtual-bg cutout quality | 10 | edge flicker between frames, halo score around face mask | B516 "clarity issues, noisy cutout" |
| Interactive/varied content (screen-share share, played-video segments) | 8 | layout classes; played video = lesson-site URL/player UI + B2 "Audio shared" | B491 "play topic videos"; also used to *mask* B1/B2 stats |
| Kid camera engagement | 5 | kid full-frame views with face present / duration | rapport context; low weight, noisy |
| Dead air/black-screen & class bounds | 7 | black/static frames + B2 silence; first/last teacher speech | B482 4-min dead air, B441 19-min tail, Malayalam demo black screen; B558 "intro/conclusion" |

Score = weighted mean of factor sub-scores (0-1), each linearly mapped between calibrated "bad" and "good" anchors from the 10 classes. Gate: if teacher tile never detected, factors 1-5 become `null` (not 0) and fusion gets `confidence: low` plus a compliance flag only if the tile was detectable but empty.

### Sampling, cost, latency (40-90 min)
- Stage 1: 1 frame / 5 s (480-1080 frames), downscaled to 640 px wide, ffmpeg `-vf fps=1/5,scale` one pass: ~20-40 s for 60 min on CPU. Layout classification on all.
- Stage 2: on frames with a tile, 1 frame / 2 s windows for face/logo (adaptive: densify around state changes). Detector ~10 ms/frame -> <2 min total.
- Flicker metric: 3-frame bursts at 10 evenly spaced points. Whole path ~2-4 min CPU, $0. Run in parallel with AssemblyAI upload.

### Output schema
```json
{"path":"A","version":1,"score":0-100,"confidence":"high|med|low",
 "session_type":"class|demo|unknown",
 "class_bounds":{"start_s":..,"end_s":..,"dead_air_s":[[a,b]],"black_s":[[a,b]]},
 "factors":{"bg_present":{"value":0.93,"score":0-1,"weight":25,"n_frames":..,
    "evidence":[{"t":412.0,"note":"8x8 logo matched, score 0.81","crop_ref":"local-only"}]}, "...":{}},
 "flags":["NO_BRANDED_BG","FACE_NOT_VISIBLE","CAMERA_OFF_WHILE_TEACHING","LOW_LIGHT","NOISY_CUTOUT","TAIL_AFTER_CLASS"],
 "timeline":[{"from":0,"to":300,"layout":"speaker|screenshare|black|tile_absent","teacher_tile":true}]}
```
Evidence has timestamps and text only; `crop_ref` is a local path, never uploaded; the app UI may show crops to the logged-in uploader only.

### Failure modes
Teacher tile mis-identified (kid speaking, parent device sharing name); tile absent during ChessKid video (don't penalise); logo template varies in scale/compression; hair/face partial detection vs "face not visible" (require N consecutive frames); screen-share window of lichess includes faces in avatars (restrict detection to tile ROI); dark skin tones lower detector recall (use low threshold, validate); Zoom recording without tile at all (labelled unknown, low confidence); unseen different branded background version.

### Validation on 10 graded classes (expected)
- B441: face_visible low, bg present, tail_after_class flag (~19 min).
- B392: bg_present near 0 -> NO_BRANDED_BG; others normal.
- B482: camera_on <100% while teaching, dead_air ~4 min at start.
- B435a: lowest lighting score of set; bg present.
- B516: highest cutout-noise score; bg present.
- B491/B472/B552: bg and face high, no flags (exc. B491 plays videos: interactive factor high).
- B558: bg present, played video masked, intro missing (B1's job; A only shows 35 min video).
- B435g: long silent play must NOT trigger dead-air (only silent + static + no screen activity).
- Demos: session_type=demo; Malayalam: black_s >= 7 min.
Pass criteria: each expected flag fires on its class and on no excellent class; Path A score rank-orders {B392,B441,B482} below {B491,B472,B552}. With n=10, report as sanity checks, not fitted accuracy; thresholds set by eyeballing anchors, then frozen.

<a id="path_B1"></a>
> **Revisions 2–3:** the judge is Gemini (text only), and the transcript comes from the uploaded .vtt or from Gemini (Part 1 §4T). AssemblyAI and Claude are no longer used. The cost figures below are outdated.
## Path B1: transcript-only qualitative scoring

Grounding: Zoom VTTs are 4-12.5k words (about 7-20k tokens with timestamps/labels), so one 60-90 min class fits in a single call. Homework appears mid-class (B472 at 45-47 of 56 min), so never window to the "closing".

### 1. Pipeline
1. **AssemblyAI** (speaker_labels, word timestamps, language_detection; set expected speakers 2-6). Audio goes to AssemblyAI only (permitted per user's lock). Keep word-level JSON locally.
2. **Teacher identification** (not "most talk"). Score each speaker: (a) first speaker after any gap who greets/says "let's begin/wait for friends"; (b) ratio of imperatives + questions + praise tokens; (c) speaks in long turns *and* is addressed by kids as "sir/ma'am/teacher"; (d) Zoom VTT display name, if supplied, as a prior. **Played-video detector**: a speaker with a single continuous turn >90 s with no pauses/questions, scripted register (no second-person address to named kids), or same voice as no one else, is labelled `VIDEO` and removed from teacher stats. Ambiguous or merged parent+child speaker -> `kid_or_parent`, flagged `diarization_confidence=low` and the score caps confidence, not value.
3. **Segmentation** (rule-based on timestamps + speaker roles, LLM only fills labels): `pre_start` (before first teacher speech), `opening` (first teacher speech until first content instruction), `teaching` (teacher+kid turns alternating), `silent_play` (gap >60 s with <3 turns/min), `video` (VIDEO spans), `closing` (last teacher turns incl. goodbyes/HW), `tail` (after last teacher word). Tail and pre_start are excluded from rates but pre_start length feeds "time to first speech".
4. **Name redaction** (before any LLM): chosen approach is *local pseudonymisation*, not AssemblyAI redaction. AssemblyAI `redact_pii` person_name would replace with `[PERSON_NAME]`, making "calls each kid individually" unmeasurable and it misses ASR-garbled names. Instead: build a roster from Zoom display names plus AssemblyAI entity detection (person_name) plus a local NER pass (spaCy/regex on vocative patterns "X, what do you think", "Good job X") with fuzzy matching (Levenshtein/phonetic) to merge garbled variants into clusters; map each cluster to stable `Kid1..KidN`; teacher -> `Teacher`; also scrub emails, phones, schools, locations. Mapping table stays local, never sent. Fail-closed: if any capitalised non-dictionary token remains near a vocative pattern, replace it with `KidX?` rather than pass. Residual-leak check: grep final text for all roster tokens before the API call; abort on a hit.
5. **Features** (deterministic, section 3) computed from pseudonymised transcript.
6. **LLM judge** (section 4), then **verifier**, then weighted score.

### 2. Factors, weights, scale
Scale 0-4 anchored; `null` (not observable) is allowed and weight renormalised. Weights sum to 100 and are tuned to grader themes.

| # | Factor | Wt | Anchors (0 / 2 / 4) | Evidence required |
|---|---|---|---|---|
| F1 | Rapport and intro | 15 | 0 jumps straight to revision / 2 greets, no personal talk / 4 greets kids individually + small talk ("how was your day") | 2 quotes in opening |
| F2 | Conclusion | 10 | 0 teacher/class just ends / 2 says bye / 4 recaps, praises, previews next, farewells to all | quote(s) in closing |
| F3 | Individual inclusion and spread | 20 | 0 one kid dominates or kids unaddressed / 2 some named questions / 4 each kid addressed by name repeatedly, turns balanced (LLM reads features + quotes) | 3 quotes across time, per-kid |
| F4 | New-joiner self-intro | 8 | 0 new kid, no intro / 2 teacher introduces / 4 invites new kid to introduce self; `null` if no new joiner | quote |
| F5 | Homework anywhere | 10 | 0 none / 2 vague / 4 specific task + how/where to submit | quote with timestamp (any segment) |
| F6 | Questioning quality | 12 | 0 recitation / 2 closed Qs / 4 open, probing, wait-time, follow-ups | 2 Q+answer pairs |
| F7 | Energy, praise, warmth | 15 | 0 flat, no praise / 2 occasional praise / 4 varied, specific praise, enthusiastic language, humour | 3 quotes |
| F8 | Clarity of explanation | 7 | 0 confusing/ repeated misunderstanding / 4 stepwise, checks understanding | 1-2 quotes |
| F9 | Tech-issue handling | 3 | 0 ignores / 4 diagnoses, communicates, recovers; `null` if none | quote |

Rationale: grader comments are dominated by rapport, individual inclusion, homework, pleasantness; chess-content correctness is deliberately unscored. F7 is a text-only proxy (tone lives in audio, B2/A). Compliance (background, camera) is out of B1's scope.

### 3. Hybrid split
**Deterministic (computed, passed to LLM as facts, also own sub-scores for fusion):** time to first teacher speech; kid turns/min and teacher Q-marks/min *within `teaching` only*; distinct kids addressed (vocative count per KidN); Gini of kid word share and of teacher-addressed counts; max kid share; teacher words per kid utterance; homework keyword hits (English + Hinglish lexicon: homework, assignment, practice, "at home", submit, ChessKid task) with timestamps as a recall aid; farewell tokens in closing; praise lexicon rate; `video_pct`, `tail_min`.
**LLM-judged:** F1, F2, F4, F6, F7, F8, F9 fully; F3 and F5 judged *with* features and keyword hits as hints (LLM can overrule but must cite). F3 final = 0.5 deterministic map (calibrated thresholds, not LLM) + 0.5 LLM, because "individual calling" is exactly where regex failed on Zoom names (pseudonyms fix that).

**Grounding:** output schema requires `evidence: [{t_start, speaker, quote}]`. Verifier: quote must be a normalised substring of the transcript at that timestamp (+-5 s) and speaker; failed evidence -> factor re-asked once, else score set to `null` and flagged. Numbers reported to user cite only verified quotes.

### 4. Prompting, model, cost
- **One call**, full pseudonymised transcript as `[mm:ss] Speaker(role/segment): text`, segment markers and feature table prepended. Per-segment calls lose cross-class context (spread, new joiner); skip. Stable prefix (rubric, anchors, few-shot, schema) first with prompt caching, transcript after; a second run (self-consistency, or re-ask of failed factors) hits the cache.
- Strict JSON via structured output/tool schema; temperature 0; reasoning field per factor *before* score.
- Model: Sonnet-class (the current Sonnet) as judge. Haiku-class for cheap redaction-leak second check. Opus-class only as optional adjudicator on low-confidence runs.
- Tokens: ~20k in (+3k cached rubric), ~3k out. Estimated about $0.10-0.15/class at Sonnet pricing, <$0.05 on cached rerun; AssemblyAI about $0.2-0.5 per class. Verify current prices before quoting. Latency: transcription ~1/5-1/10 real time (5-10 min), LLM 30-60 s.

### 5. Non-English, code-mix, demo
- language_detection per class; for Hinglish/Hindi/Tamil/Malayalam use AssemblyAI multilingual/`language_code` best model; compute `asr_confidence` (mean word confidence, % non-Latin tokens, words/min).
- If mean confidence <0.6 or words/min <30 in teaching: output `status=insufficient_transcript`, B1 contributes zero weight in fusion (do not fabricate). Malayalam demo (7 min black screen, unusable ASR) must hit this path.
- LLM prompted that code-mixed lines are normal; judge from translatable parts, quote originals.
- **Demo detection**: parent speaker present, age/schedule/fee/enrol vocabulary, 1:1 (one kid + parent), short teaching share. Classify `session_type in {class, demo}` (LLM + rule). Demo uses a different, lighter rubric (rapport, parent handling, clarity) and is reported separately, not mapped to Poor..Excellent on the class scale.

### 6. Output JSON
```
{session_type, asr:{confidence,language,status}, teacher_speaker, segments:[{type,start,end}],
 features:{...}, factors:[{id,score|null,weight,confidence,reasoning,evidence:[{t,speaker,quote,verified}]}],
 content_score_0_100, flags:[...]}
```
Score = sum(w*score/4) / sum(w of non-null) *100.

### 7. Failure modes
Diarization merges parent+child (flag, don't penalise F3); video attributed to teacher (VIDEO detector; fallback: LLM told "long monologues may be video"); name leak (fail-closed + grep); roster miss (fewer kids -> F3 inflated; cross-check with distinct speaker count); LLM praise inflation (anchors + few-shot negatives); hallucinated quotes (verifier); API/JSON failure (retry, then partial result).

### 8. Validation (write expectations first)
Before any run, commit `expectations.csv`: per class, per factor expected band from grader lines (e.g. B482 F1<=1, F4<=1; B558 F1<=1, F2<=1, F4<=1; B472 F1>=3, F5>=3; B435g F5<=1; B552 F5>=3; B392/B441 F1<=1). Only about 25 labels exist (F6, F8, F9 have none); report agreement only for graded factors, accept +-1 band, and treat the rest as sanity (inspect evidence manually). Report per-factor hit rate vs majority baseline (laya lesson: 15/21 = baseline). Also run each class 3x and report score variance; ablate (LLM without features) to show features help. Check final ordering Exc > Good > Avg > Weak, expecting overlap Good/Avg (B435 batch confound). Do not tune anchors after seeing results without logging it.

<a id="path_B2"></a>
## Path B2: audio quality / delivery from raw audio (local only)

### 1. Probe findings (all 12 files, 16 kHz mono decode, 30 ms frames; ~19 s total CPU on M-series; script: design/probe/b2_probe.py, results b2_probe.json)
- **Zoom has already gated/denoised the audio.** The 10th-percentile frame level is about -82 dB or digital zero (-120 dB in B435g). 10-59% of frames are digital silence. So a classic noise-floor SNR is meaningless and most hum/noise factors will read "clean" for every file. Hum band (40-130 Hz) energy ratio is about 6% where measurable (no discrimination).
- **Speech-level (p90 frame dB) is the one real separator:** B482 (Weak) -23.4 vs -13.5 to -18.6 for all others (B558 -18.1, B516 -18.6). It matches the "mean dB -28.9" recon and is a legitimate "teacher mic too quiet / too far" signal. It is NOT a label predictor in general: 9 of 10 classes sit within 5 dB.
- **Clipping-frame fraction** (>0.98 FS): 0.01-1.5%. Highest in Exc B472 (1.5%) and QI B441 (1.3%). Not predictive. Treat as flag only above ~3%.
- **Crude autocorrelation pitch (mixed speakers, octave errors):** f0 std 3.7-7.5 semitones, no ordering by label (Exc 3.7/5.0, Weak 5.1/7.5). Proves pitch on mixed audio is noise. It only works on teacher-only segments.
- Block-level loudness std 1.0-3.7 dB; highest in B482 (3.65) and B435g (3.2), both are the quiet or silent-play classes. Weak signal.
- Honest conclusion: raw-audio features will not separate Exc/Good/Avg/Weak. The grader's energy/pleasantness comments are about tone and affect. Signal proxies (pitch range, rate, loudness) are weak. B2 should carry low fusion weight (suggest 10-15%) and mostly act as a defect detector plus an "energy" evidence supplier.

### 2. Factors and weights (B2 score 0-100)
| factor | wt | measure | rubric tie | predictive? |
|---|---|---|---|---|
| Teacher vocal liveliness | 30 | f0 std (semitones, 10-90 pct range) and f0-range, RMS dynamic range, within teacher voiced frames only; per-class percentile vs reference table | "needs more energetic / pleasant / happy interaction" (B435a, B558, B392, B441, B552) | Plausible but unproven. Needs teacher-only turns |
| Speaking rate and variation | 10 | teacher words/sec from B1 word timestamps (no signal work); penalise too flat (CV low) or <1.5 w/s while teaching | same | Weak |
| Teacher level and consistency | 20 | median teacher speech dB (LUFS-S on teacher turns) vs absolute band -26..-14 dBFS; 60 s block std | B482 | Only B482-type failures |
| Intelligibility | 15 | optional DNSMOS/SQUIM score on 20 sampled 10 s teacher windows; fallback: teacher speech-to-gap ratio | "some clarity issues" (B516, noisy cutout) | Unknown; B516 bitrate is video, not audio |
| Dead air inside teaching | 15 | gaps >6 s with no speech inside teaching windows (see 4); report count and total s | B482 "4 min dead air", no rapport | Yes for B482, B558/B435a late starts |
| Overlap / interruption | 5 | simultaneous-speaker seconds from diarization overlaps; high overlap = chaos, near zero = no interaction | weak | Weak |
| Defects (clip >3%, dropouts, echo) | 5 | digital-silence gaps *inside* a speech turn (<1 s), clipping, echo via comb check on teacher onsets | "warned about quality issues" (B392) | Rare |
Defects are also emitted as flags independent of the score. Never penalise silence%, mean dB, or bitrate.

### 3. Speaker turns, played video, silent play
- **Preferred: reuse AssemblyAI utterances/words from B1** (speaker label, start/end ms, confidence). Zero extra compute and the same speaker ids B1 uses. Teacher = speaker with the most talk time in first 3 min, cross-checked against Zoom VTT name if available. Cost: B2 depends on B1 and inherits diarization errors (parent plus child on one device).
- **Local fallback / validator:** silero-vad (ONNX, about 2 MB, ~1-2 min per hour on CPU) for speech/non-speech, to cross-check diarization and to give timestamps without the cloud. Skip pyannote (heavy, 1 GB plus HF token, ~0.5x realtime on CPU). Dismiss clustering locally; kids and teacher share one mixed channel anyway.
- **Played lesson videos:** (a) Zoom VTT/AssemblyAI mark the speaker as "Audio shared by..." or long single-speaker monologue >90 s with no gaps and a constant spectrum; (b) detect "shared-computer-audio" by a sudden level step (>6 dB) and a drop in dynamic range plus stable spectral centroid. Mark segment type `media` and exclude from all features; report minutes excluded. Excluded B558 ~35 min must be handled or pitch/rate become the narrator's.
- **Teaching window** = from first teacher utterance to last teacher utterance (clips B441's 19 min tail, B482's pre-roll), minus `media` segments, minus intervals where the video-side (path A) or transcript says arena/puzzle play (teacher silent >60 s with kid turns or board activity). Dead air is counted only in gaps >6 s that are not within a long silent-play block (>60 s). That block threshold is a heuristic. Ask path A for screen state if available.

### 4. Tooling and cost (hosted small CPU box)
| need | tool | install | CPU for 60-90 min |
|---|---|---|---|
| decode, loudness, silence | ffmpeg (ebur128, silencedetect, astats), already local | 0 | ~10 s |
| frame RMS, clipping, turn-masking | numpy | 0 (laya-venv has it) | ~10 s (probe above) |
| pitch on teacher voiced frames | pyworld or librosa.pyin; fallback torchcrepe-free numpy autocorr with voicing threshold plus octave jump filter | ~20-60 MB | pyin ~1-3 min on 20 min of teacher speech; pyworld harvest ~1 min |
| VAD | silero-vad ONNX | ~5 MB w/ onnxruntime | ~1-2 min |
| clarity | DNSMOS (onnx) on 20x10 s samples | ~30 MB | ~10 s |
Avoid torchaudio-SQUIM and pyannote in the hosted image (torch + weights > 500 MB). Total target: <3 min added wall time, no GPU, nothing uploaded.

### 5. Normalisation
Use absolute bands for level/defects (dBFS bands are physical). For liveliness/prosody use a fixed reference table (percentiles) built from the 10 labelled classes, plus a clamp to avoid overfitting. Per-speaker semitone normalisation for f0 is required (male/female differences). Do not z-score per class (a quiet class would be flattered); state n=10 means thresholds are provisional.

### 6. I/O schema
In: `audio.wav(16k mono)`, `turns: [{spk,start,end,words,conf}]`, `teacher_spk`, `segments: [{type: teaching|media|play|tail, start, end}]`.
Out:
```json
{"score":0-100,"band":"low|ok|high","confidence":0-1,
 "factors":{"liveliness":{"score":..,"f0_range_st":..,"rms_dyn_db":..,"pct":..},"level":{"teacher_lufs":..},
  "dead_air":{"count":..,"total_s":..,"spans":[[s,e]]},"overlap_s":..,"clarity":{"dnsmos":..},
  "defects":{"clip_pct":..,"dropouts":[..]}},
 "excluded":{"media_min":..,"play_min":..,"tail_min":..},
 "evidence":[{"t":"12:41","kind":"dead_air","text":"14 s no speech during teaching"}]}
```

### 7. Failure modes
Diarization mislabels teacher (parent/child shared device) -> prosody of kid; mitigate: require teacher-speaker talk-time >=25% and f0 median sanity, else confidence low. Zoom suppression flattens noise and pitch-level cues; played videos missed -> inflated liveliness; Malayalam/other-language demo has no ASR turns -> fall back to VAD-only, mark confidence 0.3; very short sessions; mixed-down stereo with one empty channel.

### 8. Validation on the 10 graded classes
Only B482 (level -23 dB, high block std, 4 min dead air) is expected to score clearly low from B2. Expected rank-order check: B2 should place B472/B491/B552 in the upper half and B482 lowest. Success criterion: no Excellent class below 60, B482 flagged on level plus dead air. Everything else (B435a vs B435g, B558) cannot be validated from probe data and should be reported as "liveliness: unverified".

### 9. Open questions for user
1. Accept B2 as a low-weight (10-15%) plus defect-flag module? 2. Is local VAD fallback needed, or depend on B1 diarization? 3. Can path A give screen-state (arena/puzzle) for silent-play exclusion? 4. Allowed to ship DNSMOS/pyworld weights in the hosted image?

<a id="fusion"></a>
## Fusion, Thresholds, Calibration, Explanation

### 1. Recommendation: gated weighted score, LLM explains but does not grade
Three layers, all deterministic except the writing:
1. **Weighted score** (0-100) from fixed rubric-derived priors.
2. **Gates/caps** (rule-based) for the compliance failures the graders name.
3. **Ordinal cutpoints** map the capped score to Poor/Average/Good/Excellent.
A final **LLM writer** turns sub-scores + evidence into prose. It never changes the grade.

Why not an LLM adjudicator: with n=8 we cannot validate its judgement, it is non-reproducible, and it tends to echo whichever path sounds most fluent. Why not a learned model: 8 points. Fixed priors plus 2-3 cutpoints is the most we can responsibly tune.

### 2. Weights (priors, never fitted)
Grader lines are mostly interaction/rapport/homework, then background/camera/light, then energy.

| Path | Weight | Factors inside the path (sum to 1) |
|---|---|---|
| B1 transcript | 0.55 | rapport/intro .20, individual inclusion .20, conclusion .10, new-joiner self-intro .10, homework .10, questioning .10, warmth/praise .10, clarity .10 |
| A video | 0.30 | branded bg .35, face visible/camera on .30, lighting .15, screen-share/interactive content .10, dead/black screen .10 |
| B2 audio | 0.15 | vocal energy/prosody .40, clarity/SNR .30, noise .15, in-teaching dead air .15 |

Energy is graded by two paths (B1 warmth plus B2 prosody), which matches its grader frequency. Homework is only 10% of B1: Excellent B491 had none, so it is a bonus, not a requirement. Silence is counted only inside teaching segments (silent arena play is legitimate: B435g 59%, B482 61%). Teacher talk, question and praise counts exclude played lesson-video audio.

**Missing paths.** Run only paths with `status=ok`. Renormalize the remaining weights. Each missing path lowers confidence one notch (A missing: also drop the compliance-gate verdict to "unverified"). If B1 is missing (ASR garbage), abstain; B1 carries 55% of the signal and nothing else covers rapport.
- ASR garbage triggers: <40 words/min, >30% unintelligible, or non-English share >30%.
- A failure: no teacher tile found in >50% of sampled frames.

### 3. Gates (the quality-issue folder)
- Branded bg absent in >50% of sampled teacher-tile frames: cap at Poor, flag `compliance:no_branded_background` (B392).
- Face visible in <50% of teaching-time frames: cap at Poor, flag `compliance:face_not_visible` (B441). Camera off only sometimes (B482): no cap, penalty via A score and an improvement note.
- Weak B558 and B482 both kept the background, so the gate must not fire on them. Weak vs Average is separated by B1 (intro, conclusion, self-intro), not by A.
- Gates only cap downward; they never raise a grade.

QI maps to Poor plus a `compliance` flag. weak maps to Poor.

### 4. Thresholds without overfitting
Initial prior cutpoints on the capped score: Poor <50, Average 50-67, Good 68-83, Excellent >=84. Set from the rubric sub-score anchors, with a score of ~50 meaning "half the rubric items evidenced". They are set before looking at the 8 classes.

Then:
1. Run the 8 graded + 2 QI classes (10 total) with frozen weights. Print the table: sub-scores, fused score, expected ordinal (Poor=0 ... Excellent=3).
2. **Leave-one-out only on the cutpoints** (3 numbers, constrained monotonic, each within +/-8 of its prior). For each held-out class, choose the cutpoints that minimise MAE on the other 9, then predict the held-out one. Do not tune weights or factor definitions against these labels.
3. Metrics: exact accuracy, within-one accuracy, MAE on 0-3, plus Spearman of fused score vs ordinal. Report the confusion matrix. Pairs share a label, so the within-pair gap is a sanity check.
4. Ship the priors unless LOO beats them by >=1 exact hit; otherwise keep the priors and say so.

**Targets:** exact >=6/10, within-one 10/10, MAE <=0.5, both QI -> Poor with flag.

**Change the design if:**
- Spearman <0.5, or A/B2 sub-scores nearly constant across classes (they then become gates/confidence only, with B1 carrying the score);
- Good/Average are indistinguishable (grader differences there are subtle, e.g. "more pleasant interaction"), in which case merge to a score band and say "Average-Good" with low confidence;
- any QI misses the gate, in which case fix the A detectors (not the threshold);
- B1 sub-scores disagree with graders on rapport/individual inclusion (check evidence quotes first).
Be honest in the writeup: 10 classes validates sanity and ordering, not accuracy.

### 5. Confidence, abstain, session type
`confidence` in {high, medium, low}:
- Start at high.
- Down one notch if: a path is missing; fused score within 4 pts of a cutpoint; paths disagree (A/B1/B2 normalized scores span >30 pts); ASR quality marginal.
- Gate verdicts based on <8 usable frames: low.

**Session-type check runs first** (cheap, before scoring). Signals: B1 classifier for parent present, age/onboarding questions, scheduling/enrolment talk; 1 kid; duration <~35 min or non-English share high; long black screen. If `trial_demo` is likely: output `session_type: "demo"`, `grade: null`, state "This looks like a 1:1 trial/demo session, not a regular class. Class grading was not applied", and show only observations (greeting, background, clarity). A demo with unusable ASR (Malayalam) is `session_type: "unknown", abstain`.

**Abstain** (`grade: null`, reason shown) when: B1 failed; duration <15 min; the recording is mostly not a class (black screen >40%); or session type unknown with low confidence.

### 6. Explanation: every claim traceable
- Each upstream path emits `evidence[]`: `{id, path, factor, t_start, t_end, quote_or_observation, polarity, severity}`. The quote is redacted text, or a vision/audio descriptor (e.g. "branded bg present 0:00-52:10").
- The writer LLM receives only sub-scores plus this evidence list (no transcript, no frames). It must output bullets as `{text, evidence_ids[]}`.
- **Validator** (code): every bullet has >=1 valid id; timestamps in text equal the cited evidence; polarity matches (a strength cites positive evidence); no bullets with no ids are shown; kid-name regex/NER pass against the redaction map on the output, with a failed bullet dropped or regenerated once.
- Selection is deterministic: top 3 strengths and top 3 improvements by weight x |gap|; the compliance gate is always first when fired.
- Voice follows the grader lines: short, specific, actionable ("Greet each student before starting the puzzles", "Homework not given by 46:00"). Refer to kids as "a student" or "Student 2".

### 7. Output JSON
```json
{
 "session_type": "regular|demo|unknown",
 "grade": "Poor|Average|Good|Excellent|null",
 "score": 71.4, "score_uncapped": 71.4,
 "confidence": "high|medium|low", "abstain_reason": null,
 "flags": [{"code":"compliance:no_branded_background","evidence_ids":["A-03"]}],
 "paths": {"A":{"status":"ok","score":80,"weight_used":0.3,"factors":{"branded_bg":{"score":100,"evidence_ids":["A-01"]}}},
           "B1":{...},"B2":{...}},
 "strengths":[{"text":"Greeted each student individually at 0:21","evidence_ids":["B1-02"]}],
 "improvements":[{"text":"No homework given by 46:00","evidence_ids":["B1-11"]}],
 "evidence":[{"id":"B1-02","path":"B1","factor":"rapport","t_start":21,"t_end":48,"text":"\"how was your day, [Student 1]?\"","polarity":"+"}],
 "calibration":{"cutpoints":[50,68,84],"version":"v1","caveat":"validated on 10 classes"},
 "nearest_boundary":{"to":"Good","delta":2.6}
}
```
Weights and cutpoints live in one versioned config file.

<a id="devils_advocate"></a>
> **Revisions 2–3:** AssemblyAI and Claude have been dropped. Gemini is now the only external provider; the user approved this, and organiser approval is still open.
## Devil's Advocate: ranked holes and mitigations

(Vendor limits below are from memory, not re-verified today; check before citing.)

### 1. [CRITICAL] Compliance: "services specifically permitted" has an empty list
- AssemblyAI is NOT permitted by default. Raw audio of children's voices goes to a third party. Redacting names in text later does not help; the voice leaves at upload. Redaction after ASR also misses ASR-garbled names (e.g. a child's name transcribed as a common word).
- Any hosted video API (Gemini/GPT-4o video) is worse: faces plus voices.
- The Zoom .vtt already exists for the 12 training recordings, but a grader's unseen upload has none.
- Hosted app: uploads land on a public PaaS disk; Streamlit/Gradio logs, temp dirs, and AssemblyAI/Anthropic retention (typically stored until deleted or ~30 days) persist copies.
- Least-risk configuration:
  1. Ask the organiser in writing today whether AssemblyAI and Anthropic are permitted. Frame it as: "We need X to be allowed; otherwise we fall back to local Whisper."
  2. Ship a switchable ASR backend: local faster-whisper (CPU small/base) as the default-safe fallback, AssemblyAI behind a flag only if approved.
  3. Anthropic receives only redacted text and numeric features, never audio or frames.
  4. Path A sends nothing outbound: local OpenCV/ONNX only (face present, 8x8 logo, camera-off). No multimodal LLM on frames.
  5. Delete uploads in a `finally` block; call AssemblyAI's transcript-delete after use; no filenames or transcripts in logs; show a consent banner.
  6. Do not commit the data. AI interaction logs must also be scrubbed of kid names and transcript quotes.
- Fallback if the organiser doesn't answer by Friday: ship local-only and say so in the writeup. That is a defensible decision, and "I asked" is scored under problem-solving.

### 2. [CRITICAL] Hosted box cannot do this in a grader's patience window
- Streamlit Community Cloud: upload cap is 200 MB by default (configurable, but a 340 MB file may still hit proxy limits), roughly 1 GB RAM, shared CPU. A 340 MB mp4 breaks it. HF Spaces free CPU (16 GB) is more tolerant but cold-starts and sleeps.
- Pipeline time: ffmpeg audio extract (~1-2 min for 90 min on shared CPU), frame sampling (cheap if 1 frame per 20-30 s; do not decode every frame), AssemblyAI (~1/10 to 1/5 real time, so ~5-10 min on 90 min of audio), local Whisper on CPU (can be 1x real time, which is unusable), Claude (cheap: ~15-25k tokens redacted).
- Total is roughly 8-15 min. A grader will close the tab.
- Mitigations:
  - Accept audio-only or pre-compressed input; client-side tip: "upload the .m4a / .mp3".
  - Extract 16 kHz mono audio first (~40 MB at 90 min); never keep the full video in memory.
  - Run the pipeline as a background job with a job id, persistent progress (stages), and a result URL that survives refresh.
  - Provide an instant "Try a sample" mode with a precomputed result, plus a stage-by-stage streaming result (audio metrics in under 1 min, then content).
  - Cap analysis at the first N minutes with a clear label if the box is slow.
  - Free-tier sleep: warm it before grading and say so in the README.

### 3. [HIGH] Path-specific misleading signals (from the recon table)
- B2 raw audio: silence% ranks Good B435g (59%) below Weak B558 (22%); B482 (61%) looks the same as B435g. Mean dB and peak dB (all hit 0 dB) carry no signal. Silence is only valid inside segments labelled "teaching", which needs the transcript. So B2 is not independent of B1.
- B1 transcript: teacher talk%, "?" and praise counts are inflated by played lesson-video narration (B558 has ~35 min credited to the teacher). Zoom display names differ from spoken names; a parent-plus-child device collapses to one speaker; AssemblyAI diarization on a 2-6 person class with video audio is unstable. B441's 19 min tail and B482's 4 min dead air distort rates unless normalised to the "first to last teacher utterance" window.
- Path A vision: ~80% of frames are a chessboard, so whole-frame models see only chess. Evidence lives in the top-right tile. Branded-background detection is confounded by noisy cutouts (B516 is Average yet has the background). The batch id leaks teacher identity (B435 is Good and Average).
- Demo sessions: parent present, scheduling talk, Malayalam with a black screen; treat them as out-of-distribution (see 6).
- Mitigation: B1 needs a "played-video/narration" filter and a teaching-window normaliser; B2 is gated by B1 segments or dropped.

### 4. [HIGH] Validation on n=8 is mostly self-deception
- 21 labels, q2 only 2 positives, q4 all positive (cannot be tested), majority baseline already matches laya at 15/21. Any threshold tuned on 8 classes with 4 levels will fit by construction.
- "LLM agrees with grader" is circular if the prompt contains the grader's rubric phrases and the same classes shaped the prompt. A judge told "rapport, individual calling, homework" will find them everywhere.
- Honest validation:
  - Freeze prompt and thresholds before scoring; keep 2 classes (e.g. one Average, one Weak) never viewed until the end.
  - Report per-factor agreement against the 21 labels with the majority baseline beside it, and ordinal error (off by one level is fine, off by two is not).
  - Run the judge 3 times and report variance; shuffle factor order.
  - Check the evidence quotes exist verbatim in the transcript.
  - State plainly: n=8, indicative only. That candour scores under clarity and problem-solving.
- Do not claim "Excellent vs Good" separation; the data supports at most Poor/Average vs Good/Excellent plus compliance flags.

### 5. [HIGH] Scope: three paths plus fusion in about a day will not all be good
- Rubric weights: 35% assessment quality, 30% effective AI use, 25% working app and code, 10% clarity. A flaky hosted app loses 25 points outright.
- Signal per hour, ranked:
  1. B1 (transcript LLM, evidence-cited): carries nearly all the grader-rubric themes (rapport, intro/conclusion, individual calling, homework, tone). Highest signal.
  2. A-lite (local teacher-tile checks: face present, branded background, camera off): covers the entire "quality issue" class, which B1 cannot see. Cheap with OpenCV on sampled frames.
  3. B2 gives the least signal. The recon table shows no clean separation from silence%, mean dB or peak. Weak B482 is the only outlier on mean dB (-28.9 vs -21 to -25), and that is one sample. Keep only a quick sanity layer: audio clip/noise flag and an "unusable audio" gate. Drop energy-based scoring; "energetic tone" is better read from the transcript than from dBFS.
- Cut: full multimodal LLM Path A; fusion learned from data. Stub: fusion as a transparent rule table (compliance flags cap the grade at Poor/Average; B1 weighted score sets the band; B2 can only demote on unusable audio). Show each path's output in the UI, even when stubbed.

### 6. [MEDIUM-HIGH] The unseen recording may not look like the training set
- Demo or 1:1 sales trial: a session-type detector (parent speaking, "enrol", "schedule") must fire before scoring, and output "Demo session: rubric not applicable" or a separate rubric. Otherwise it is graded Poor for no homework.
- Non-English (Malayalam/Tamil/Hindi): set AssemblyAI language detection; if confidence is low or the transcript is garbled, return "low confidence, no grade" with partial A/B2 results rather than a confident wrong grade.
- Other subject (not chess): remove any chess-specific logic from prompts; the rubric is conduct-based.
- Short clip (under 10 min): the grade is not comparable; show "insufficient duration" and grade the available signals with a warning. Rates normalised per minute break on very short clips.
- Link input (Drive/YouTube): scraping Drive/YouTube is fragile, needs yt-dlp, may need cookies, and it is a policy risk. Accept uploads first; add a Drive public-link fetch only as a stretch, and say which is supported. Large link downloads also hit the timeout in 2.
- Failure modes (black screen, no audio, corrupt file) must produce a handled message, not a stack trace.

### 7. [MEDIUM] Explanations: fluff and fabricated quotes
- LLMs invent plausible quotes, especially from garbled ASR. Generic text such as "the teacher built good rapport" scores poorly.
- Mitigations:
  - Structured output: per factor {score, evidence: [{t_start, speaker, quote}], confidence}.
  - Code verifies that each quote is a substring (fuzzy >= 90%) of the redacted transcript and that the timestamp matches; drop or flag any that fail. Show a "not found in transcript" count.
  - Require absence-evidence explicitly ("no homework mention found in 0-56 min"), and allow "not assessable".
  - Cite redacted text (names appear as [STUDENT]); do not show un-redacted names in outputs that are logged.
  - Final explanation is templated from verified evidence; the LLM may only choose which evidence to cite, not rephrase it.

### Ordering for the day
Ask the organiser (1) first. Build the local pipeline plus B1 with verified quotes. Add A-lite. Add the fusion rule table. Harden the app (2, 6). B2 last, or as a gate only.
