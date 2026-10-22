"""Deterministic scorer: observations in, grade out (spec §5). The LLM never picks the grade."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cqa.aggregate import CAMERA_ORDER, Consensus, ItemConsensus, conservative
from cqa.rubric import DEFAULT_CUTPOINTS, STATUS_VALUE, WEIGHTS, Grade
from cqa.schema import CameraObs

MIN_GATE_FRAMES = 8
NEAR_CUTPOINT = 4.0
HIGH_AGREEMENT = 0.9
LOW_AGREEMENT = 0.7
MIN_SESSION_AGREEMENT = 2 / 3

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


def _weighted(items: dict[str, ItemConsensus], weights: dict[str, int]) -> tuple[float | None, float]:
    total_w = sum(weights.values())
    num = den = 0.0
    for item_id, w in weights.items():
        ic = items.get(item_id)
        v = STATUS_VALUE[ic.status] if ic else None
        if v is None:
            continue
        num += w * v
        den += w
    if den == 0:
        return None, 0.0
    return round(100.0 * num / den, 4), den / total_w


def _apply_gate_signals(camera: CameraObs, gates: list[GateSignal], flags: list[str]) -> CameraObs:
    fields = camera.model_dump()
    for g in gates:
        if g.n_frames < MIN_GATE_FRAMES or g.field not in CAMERA_ORDER:
            continue
        current = fields[g.field]
        if g.value != current and g.value != "unclear":
            flags.append(f"crosscheck_disagree:{g.field}")
        fields[g.field] = conservative(g.field, [current, g.value])
    return CameraObs(**fields)


def _confidence(items: dict[str, ItemConsensus], score: float | None, cfg: ScoringConfig, camera: CameraObs, flags: list[str]) -> str:
    valued = [ic.agreement for ic in items.values() if STATUS_VALUE[ic.status] is not None]
    mean_agree = sum(valued) / len(valued) if valued else 0.0
    gate_unclear = camera.branded_background == "unclear" or camera.face_visible == "unclear"
    if mean_agree < LOW_AGREEMENT or gate_unclear:
        return "low"
    near = score is None or min(abs(score - c) for c in cfg.cutpoints) <= NEAR_CUTPOINT
    noisy = any(f.startswith(("crosscheck_disagree", "unsupported_claim")) for f in flags)
    if mean_agree >= HIGH_AGREEMENT and not near and not noisy:
        return "high"
    return "medium"


def score(
    c: Consensus,
    cfg: ScoringConfig | None = None,
    gates: list[GateSignal] | None = None,
    flags: list[str] | None = None,
) -> Assessment:
    cfg = cfg or ScoringConfig()
    flags = list(flags or [])
    camera = _apply_gate_signals(c.camera, gates or [], flags)
    value, coverage = _weighted(c.items, cfg.weights)

    grade: Grade | None = None
    abstain: str | None = None
    session = c.session_type

    if c.session_agreement < MIN_SESSION_AGREEMENT:
        abstain = "session_unclear"
    elif session == "demo_trial":
        session = "demo"
    elif session in ("not_a_class", "unknown"):
        abstain = "not_a_class"
    elif value is None or coverage < cfg.coverage_min:
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

    return Assessment(
        grade=grade,
        score=value,
        session_type=session,
        confidence=_confidence(c.items, value, cfg, camera, flags),
        flags=flags,
        abstain_reason=abstain,
        items=c.items,
        camera=camera,
        coverage=round(coverage, 4),
    )
