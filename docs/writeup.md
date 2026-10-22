# Writeup

**Approach.** The recordings are 8x8 online chess classes. The grader's feedback is about how the class is delivered, not the chess: rapport, calling on each child, energy, homework, the closing, introducing new joiners, and camera plus the 8x8 background. I turned it into a 7-item checklist. Gemini Flash watches and listens to the whole class (plus closer passes on the first and last 10 minutes) and marks each item met, partial or not met with a timestamp and quote. Code, not the model, decides the grade: it checks evidence, applies fixed weights, caps compliance problems (no branded background, face not visible), detects trial demos, and applies cutpoints. A separate transcript judge (B1) scores pseudonymised transcripts (Kid1, Kid2); unit-tested, not yet run live.

**What worked.** Recon exposed misleading signals: silence (silent lichess play is legitimate) and talk counts (played lesson videos). A local classifier, laya, only tied a majority-class baseline (15/21 item labels), so I dropped it. On Excellent class B472, Gemini's full-video observation produced Excellent (95/100) with quotes; 1 of 12 evidence items failed validation.

**What failed.** A 228 MB upload was unreliable, so recordings upload as a small proxy. The key turned out to be free-tier and hit per-minute token limits; I added backoff and timeouts. Audio liveliness didn't separate classes.

**Limitations.** Grade accuracy rests on 10 labelled classes. Item labels, pairwise ranking and counterfactual video edits (cut homework, black out the camera tile) only support the mechanism. My transcript-only annotations matched the grader 51% strictly and 91% leniently, a realistic ceiling. One live run isn't an evaluation.

**Privacy.** Recordings show children: never committed, uploads are deleted after review, and the model is told never to name children. They went to Gemini on a free-tier key, whose terms may allow data use; production needs paid tier and organiser approval.
