"""Merge the per-run Gemini calls and vote across self-consistency runs (spec §4.2-4.3)."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from pydantic import BaseModel, Field

from cqa.evidence import parse_ts
from cqa.rubric import ITEM_IDS, ORDINAL, Status
from cqa.schema import CameraObs, Evidence, ItemObservation, ObserverOutput, SessionType

NULL_STATUSES = (Status.NOT_APPLICABLE, Status.NOT_OBSERVABLE)
EVIDENCE_DEDUP_S = 10.0

# Conservative-first orderings for camera sub-observations ("unclear" handled separately).
CAMERA_ORDER: dict[str, tuple[str, ...]] = {
    "branded_background": ("absent", "present"),
    "face_visible": ("rarely", "sometimes", "mostly"),
    "camera_on_while_teaching": ("rarely", "sometimes", "mostly"),
    "lighting": ("dim", "ok"),
}
SESSION_PRIORITY: tuple[str, ...] = ("demo_trial", "not_a_class", "unknown", "regular_class")


class MergedRun(BaseModel):
    items: dict[str, ItemObservation]
    camera: CameraObs = Field(default_factory=CameraObs)
    session_type: SessionType | None = None
    class_start: str | None = None
    class_end: str | None = None


class ModuleVote(BaseModel):
    module: str
    item: str
    status: Status
    weight: float = 1.0
    evidence: list[Evidence] = Field(default_factory=list)


class ItemConsensus(BaseModel):
    status: Status
    agreement: float
    evidence: list[Evidence] = Field(default_factory=list)
    votes: list[Status] = Field(default_factory=list)


class Consensus(BaseModel):
    items: dict[str, ItemConsensus]
    camera: CameraObs
    session_type: SessionType
    session_agreement: float


def _not_observable() -> ItemObservation:
    return ItemObservation(status=Status.NOT_OBSERVABLE, evidence=[], rationale="not reported")


def _valued(o: ItemObservation | None) -> bool:
    return o is not None and o.status not in NULL_STATUSES


def _answered(o: ItemObservation | None) -> bool:
    return o is not None and o.status != Status.NOT_OBSERVABLE


def conservative(field: str, values: Iterable[str]) -> str:
    """Most conservative non-'unclear' value; 'unclear' only if nothing else."""
    order = CAMERA_ORDER[field]
    known = [v for v in values if v in order]
    if not known:
        return "unclear"
    return min(known, key=order.index)


def majority_field(field: str, values: list[str]) -> str:
    """Majority of non-'unclear' values across calls; a tie is 'unclear'. One focus window that saw a moment
    (e.g. camera not yet on in the first minutes) must not decide a whole-class compliance gate."""
    known = [v for v in values if v in CAMERA_ORDER[field]]
    if not known:
        return "unclear"
    counts = Counter(known)
    top = max(counts.values())
    winners = [v for v, n in counts.items() if n == top]
    return winners[0] if len(winners) == 1 else "unclear"


def merge_calls(
    main: ObserverOutput,
    opening: ObserverOutput | None,
    closing: ObserverOutput | None,
) -> MergedRun:
    calls = [c for c in (main, opening, closing) if c is not None]
    get = lambda call, i: call.items.get(i) if call is not None else None  # noqa: E731

    items: dict[str, ItemObservation] = {i: main.items.get(i) or _not_observable() for i in ITEM_IDS}

    for item_id, focus in (("rapport_intro", opening), ("new_joiner_intro", opening), ("closing", closing)):
        o = get(focus, item_id)
        if not _answered(o):
            continue
        if o.status == Status.NOT_APPLICABLE and items[item_id].status == Status.NOT_MET:
            continue  # a focus window's "not applicable" cannot erase a main-call "not met"
        items[item_id] = o

    # Homework can happen anywhere, so a focus call (opening/closing) votes only when it cites homework evidence
    # inside its own window (evidence was already window-validated); otherwise it abstains. The main call always votes.
    hw = []
    for c in calls:
        o = c.items.get("homework")
        if not _valued(o):
            continue
        if c is main or o.evidence:
            hw.append(o)
    if hw:  # majority across voting calls; ties go to the stricter (lower) status
        counts = Counter(o.status for o in hw)
        top = max(counts.values())
        winner = min((st for st, n in counts.items() if n == top), key=lambda st: ORDINAL[st])
        items["homework"] = next(o for o in hw if o.status == winner)

    cam_items = [c.items["camera_background"] for c in calls if _valued(c.items.get("camera_background"))]
    if cam_items:
        items["camera_background"] = min(cam_items, key=lambda o: ORDINAL[o.status])

    cams = [c.camera for c in calls if c.camera is not None]
    camera = CameraObs(**{f: majority_field(f, [getattr(c, f) for c in cams]) for f in CAMERA_ORDER})

    return MergedRun(
        items=items,
        camera=camera,
        session_type=main.session_type,
        class_start=main.class_start,
        class_end=main.class_end,
    )


def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    kept: list[Evidence] = []
    seen: list[float] = []
    for ev in evidence:
        try:
            t = parse_ts(ev.t_start)
        except ValueError:
            continue
        if any(abs(t - s) < EVIDENCE_DEDUP_S for s in seen):
            continue
        seen.append(t)
        kept.append(ev)
    return kept


def _decide(weighted: list[tuple[Status, float]]) -> Status:
    total = sum(w for _, w in weighted)
    tally: Counter = Counter()
    for s, w in weighted:
        tally[s] += w
    for s, w in tally.items():
        if w > total / 2:
            return s
    valued = sorted(((s, w) for s, w in weighted if s not in NULL_STATUSES), key=lambda sw: ORDINAL[sw[0]])
    if not valued:
        return tally.most_common(1)[0][0]
    half = sum(w for _, w in valued) / 2
    acc = 0.0
    for s, w in valued:
        acc += w
        if acc >= half:
            return s
    return valued[-1][0]


def vote(runs: list[MergedRun], extra: list[ModuleVote] | None = None) -> Consensus:
    if not runs:
        raise ValueError("vote() needs at least one run")
    extra = extra or []
    items: dict[str, ItemConsensus] = {}
    for item_id in ITEM_IDS:
        voters: list[tuple[Status, float, list[Evidence]]] = [
            (r.items[item_id].status, 1.0, r.items[item_id].evidence) for r in runs
        ]
        voters += [(v.status, v.weight, v.evidence) for v in extra if v.item == item_id]
        winner = _decide([(s, w) for s, w, _ in voters])
        total = sum(w for _, w, _ in voters)
        win_w = sum(w for s, w, _ in voters if s == winner)
        evidence = _dedupe([ev for s, _, evs in voters if s == winner for ev in evs])
        items[item_id] = ItemConsensus(
            status=winner,
            agreement=win_w / total if total else 0.0,
            evidence=evidence,
            votes=[s for s, _, _ in voters],
        )

    camera_fields = {}
    for f in CAMERA_ORDER:
        counts = Counter(getattr(r.camera, f) for r in runs)
        top = max(counts.values())
        tied = [v for v, c in counts.items() if c == top]
        camera_fields[f] = tied[0] if len(tied) == 1 else conservative(f, tied)
    camera = CameraObs(**camera_fields)

    sessions = Counter(r.session_type or "unknown" for r in runs)
    top = max(sessions.values())
    session = min((s for s, c in sessions.items() if c == top), key=SESSION_PRIORITY.index)
    return Consensus(items=items, camera=camera, session_type=session, session_agreement=top / len(runs))
