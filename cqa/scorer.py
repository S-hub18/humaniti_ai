"""Deterministic scorer: observations in, grade out (spec §5). The LLM never picks the grade."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cqa.aggregate import CAMERA_ORDER, Consensus, ItemConsensus, conservative
from cqa.rubric import DEFAULT_CUTPOINTS, STATUS_VALUE, WEIGHTS, Grade, Status
from cqa.schema import CameraObs

MIN_GATE_FRAMES = 8
NEAR_CUTPOINT = 4.0
HIGH_AGREEMENT = 0.9
LOW_AGREEMENT = 0.7
MIN_SESSION_AGREEMENT = 2 / 3
# An item with no usable evidence (not observed, or its claim dropped by validation) is scored at a neutral
# prior instead of vanishing from the average: dropping a partial cannot raise the score, dropping a met lowers it.
NEUTRAL_PRIOR = 0.5
# Grading needs at least 5 of the 7 checklist items actually observed. With 7 items, 5 guarantees most of the
# rubric is evidenced: at most two items may be unknown (e.g. new joiners not applicable + one unobservable), and
# no single item (max weight 20) can swing the grade by more than one band on its own.
MIN_GRADED_ITEMS = 5
NA_ALLOWED = {"new_joiner_intro"}
# Not-met cap (from the rubric, not fitted to labels): each not_met item is a checklist criterion the class clearly
# failed. "Good" means most of the checklist is met, so two clear failures cap the grade at Average; three failures
# means about half of the six applicable items failed, which is Poor whatever the weighted score says.
CAP_AVERAGE_NOT_MET = 2
CAP_POOR_NOT_MET = 3  # only item the rubric allows to be "not applicable"

CameraField = Literal["branded_background", "face_visible", "camera_on_while_teaching", "lighting"]


class GateSignal(BaseModel):
    module: str
    field: CameraField
    value: str
    n_frames: int


class ScoringConfig(BaseModel):
    cutpoints: tuple[float, float, float] = DEFAULT_CUTPOINTS
    coverage_min: float = 0.6
    weights: dict[str, int] = Field(default_factory=lambda: dict(WEIGHTS))


class Assessment(BaseModel):
    grade: Grade | None
    score: float | None
    session_type: str
    confidence: Literal["high", "medium", "low"]
    flags: list[str]
    abstain_reason: str | None
    items: dict[str, ItemConsensus]
    camera: CameraObs
    coverage: float


def grade_from_score(score: float, cutpoints: tuple[float, float, float]) -> Grade:
    avg, good, exc = cutpoints
    if score >= exc:
        return Grade.EXCELLENT
    if score >= good:
        return Grade.GOOD
    if score >= avg:
        return Grade.AVERAGE
    return Grade.POOR


def _weighted(items: dict[str, ItemConsensus], weights: dict[str, int]) -> tuple[float | None, float, int]:
    """(score, coverage, n_graded). Unobserved items count at NEUTRAL_PRIOR; only new_joiner N/A is excluded."""
    total_w = sum(weights.values())
    num = den = observed_w = 0.0
    n_graded = 0
    for item_id, w in weights.items():
        ic = items.get(item_id)
        status = ic.status if ic else None
        if status == Status.NOT_APPLICABLE and item_id in NA_ALLOWED:
            continue
        v = STATUS_VALUE[status] if status is not None else None
        if v is None:
            v = NEUTRAL_PRIOR
        else:
            observed_w += w
            n_graded += 1
        num += w * v
        den += w
    if n_graded == 0 or den == 0:
        return None, 0.0, 0
    return round(100.0 * num / den, 4), observed_w / total_w, n_graded


def _apply_gate_signals(camera: CameraObs, gates: list[GateSignal], flags: list[str]) -> CameraObs:
    """Local crosscheck: fills an 'unclear' AI value; when both sides are decisive and DISAGREE, the field becomes
    'unclear' (flag + low confidence) -- a disagreement never fires a compliance cap on its own."""
    fields = camera.model_dump()
    for g in gates:
        if g.n_frames < MIN_GATE_FRAMES or g.field not in CAMERA_ORDER or g.value == "unclear":
            continue
        current = fields[g.field]
        if current == "unclear":
            fields[g.field] = g.value
        elif g.value != current:
            flags.append(f"crosscheck_disagree:{g.field}")
            fields[g.field] = "unclear"
    return CameraObs(**fields)


def _confidence(items: dict[str, ItemConsensus], score: float | None, cfg: ScoringConfig, camera: CameraObs,
                flags: list[str], ignore_camera: bool = False) -> str:
    valued = [ic.agreement for ic in items.values() if STATUS_VALUE[ic.status] is not None]
    mean_agree = sum(valued) / len(valued) if valued else 0.0
    gate_unclear = not ignore_camera and (camera.branded_background == "unclear" or camera.face_visible == "unclear")
    gate_unclear = gate_unclear or any(f.startswith("crosscheck_disagree") for f in flags)
    if mean_agree < LOW_AGREEMENT or gate_unclear:
        return "low"
    near = score is None or min(abs(score - c) for c in cfg.cutpoints) <= NEAR_CUTPOINT
    noisy = any(f.startswith(("crosscheck_disagree", "unsupported_claim")) for f in flags)
    if mean_agree >= HIGH_AGREEMENT and not near and not noisy:
        return "high"
    return "medium"


def _cap_conf(conf: str, transcript_only: bool) -> str:
    return "medium" if transcript_only and conf == "high" else conf


def score(
    c: Consensus,
    cfg: ScoringConfig | None = None,
    gates: list[GateSignal] | None = None,
    flags: list[str] | None = None,
    transcript_only: bool = False,
) -> Assessment:
    """`transcript_only`: no video, so camera/background are unknowable: no camera gates, confidence capped at medium."""
    cfg = cfg or ScoringConfig()
    flags = list(flags or [])
    if transcript_only:
        flags.append("transcript_only:no_camera")
    camera = _apply_gate_signals(c.camera, gates or [], flags)
    value, coverage, n_graded = _weighted(c.items, cfg.weights)

    grade: Grade | None = None
    abstain: str | None = None
    session = c.session_type

    if c.session_agreement < MIN_SESSION_AGREEMENT:
        abstain = "session_unclear"
    elif session == "demo_trial":
        session = "demo"
    elif session in ("not_a_class", "unknown"):
        abstain = "not_a_class"
    elif value is None or coverage < cfg.coverage_min or n_graded < MIN_GRADED_ITEMS:
        abstain = "insufficient_observations"
    else:
        grade = grade_from_score(value, cfg.cutpoints)

    if camera.branded_background == "absent":
        flags.append("compliance:no_branded_background")
    if camera.face_visible == "rarely":
        flags.append("compliance:face_not_visible")
    if camera.camera_on_while_teaching == "sometimes":
        flags.append("improve:camera_off_at_times")
    if grade is not None and any(f.startswith("compliance:") for f in flags):
        grade = Grade.POOR  # gates only ever lower the grade; POOR is the floor
    n_not_met = sum(1 for ic in c.items.values() if ic.status == Status.NOT_MET)
    if grade is not None and n_not_met >= CAP_POOR_NOT_MET:
        flags.append("cap:three_or_more_not_met")
        grade = Grade.POOR
    elif grade is not None and n_not_met >= CAP_AVERAGE_NOT_MET and grade > Grade.AVERAGE:
        flags.append("cap:two_not_met")
        grade = Grade.AVERAGE

    return Assessment(
        grade=grade,
        score=value,
        session_type=session,
        confidence=_cap_conf(_confidence(c.items, value, cfg, camera, flags, ignore_camera=transcript_only), transcript_only),
        flags=flags,
        abstain_reason=abstain,
        items=c.items,
        camera=camera,
        coverage=round(coverage, 4),
    )
