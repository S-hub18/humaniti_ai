"""Observer interface, call planning and the mock observer (used until GEMINI_API_KEY lands)."""

from __future__ import annotations

import random
from typing import Literal, Protocol

from pydantic import BaseModel

from cqa.evidence import fmt_ts
from cqa.media import MediaRef
from cqa.rubric import CALL_ITEMS, ORDINAL, Status
from cqa.schema import CameraObs, Evidence, ItemObservation, ObserverOutput

FOCUS_S = 600.0
Resolution = Literal["low", "medium", "high"]


class CallSpec(BaseModel):
    name: Literal["main", "opening", "closing"]
    start: float
    end: float
    media_resolution: Resolution


class CallStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False


class Observer(Protocol):
    def observe(self, media: MediaRef, spec: CallSpec, sample_idx: int) -> tuple[ObserverOutput, CallStats]: ...


def plan_calls(
    duration: float,
    focus_s: float = FOCUS_S,
    class_start: float | None = None,
    class_end: float | None = None,
) -> list[CallSpec]:
    """Main call over the whole file; opening/closing focus windows anchored on the class bounds."""
    lo = max(0.0, class_start or 0.0)
    hi = min(duration, class_end if class_end is not None else duration)
    if hi <= lo:
        lo, hi = 0.0, duration
    open_end = min(hi, lo + focus_s)
    close_start = max(lo, hi - focus_s)
    return [
        CallSpec(name="main", start=0.0, end=duration, media_resolution="low"),
        CallSpec(name="opening", start=lo, end=open_end, media_resolution="high"),
        CallSpec(name="closing", start=close_start, end=hi, media_resolution="high"),
    ]


_NEIGHBOURS = {
    Status.MET: (Status.PARTIAL,),
    Status.PARTIAL: (Status.MET, Status.NOT_MET),
    Status.NOT_MET: (Status.PARTIAL,),
}
GOOD_CAMERA = CameraObs(branded_background="present", face_visible="mostly", camera_on_while_teaching="mostly", lighting="ok")


class MockObserver:
    """Scripted fixtures or an oracle-with-noise generator. Never report mock numbers as results."""

    def __init__(
        self,
        script: dict[str, list[dict]] | None = None,
        oracle: dict[str, Status] | None = None,
        noise: float = 0.0,
        seed: int = 0,
        camera: CameraObs | None = None,
        session_type: str = "regular_class",
    ) -> None:
        self.script = script
        self.oracle = oracle or {}
        self.noise = noise
        self.seed = seed
        self.camera = camera or GOOD_CAMERA
        self.session_type = session_type
        self.calls = 0

    def observe(self, media: MediaRef, spec: CallSpec, sample_idx: int) -> tuple[ObserverOutput, CallStats]:
        self.calls += 1
        if self.script is not None:
            fixtures = self.script[spec.name]
            payload = dict(fixtures[sample_idx % len(fixtures)])
            payload.setdefault("window", (spec.start, spec.end))
            payload.setdefault("call", spec.name)
            return ObserverOutput.model_validate(payload), CallStats()
        items = {}
        t = fmt_ts(min(spec.end, spec.start + 30.0))
        for item_id in CALL_ITEMS[spec.name]:
            status = self.oracle.get(item_id, Status.NOT_OBSERVABLE)
            rng = random.Random(f"{self.seed}|{spec.name}|{sample_idx}|{item_id}")
            if status in ORDINAL and rng.random() < self.noise:
                status = rng.choice(_NEIGHBOURS[status])
            ev = [Evidence(t_start=t, observation=f"mock evidence for {item_id}")] if status in (Status.MET, Status.PARTIAL) else []
            items[item_id] = ItemObservation(status=status, evidence=ev, rationale="mock")
        out = ObserverOutput(
            call=spec.name,
            window=(spec.start, spec.end),
            items=items,
            camera=self.camera,
            session_type=self.session_type if spec.name == "main" else None,
        )
        return out, CallStats()
