"""Observation schemas shared by the observer, aggregator and scorer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cqa.rubric import CALL_ITEMS, Status

Modality = Literal["visual", "audio", "both"]
SessionType = Literal["regular_class", "demo_trial", "not_a_class", "unknown"]
CallName = Literal["main", "opening", "closing"]
Presence = Literal["present", "absent", "unclear"]
Frequency = Literal["mostly", "sometimes", "rarely", "unclear"]
Lighting = Literal["ok", "dim", "unclear"]


class Evidence(BaseModel):
    t_start: str
    t_end: str | None = None
    modality: Modality = "both"
    observation: str
    quote: str | None = None


class CameraObs(BaseModel):
    branded_background: Presence = "unclear"
    face_visible: Frequency = "unclear"
    camera_on_while_teaching: Frequency = "unclear"
    lighting: Lighting = "unclear"


class ItemObservation(BaseModel):
    status: Status
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""


class ObserverOutput(BaseModel):
    call: CallName
    window: tuple[float, float]
    items: dict[str, ItemObservation]
    camera: CameraObs | None = None
    session_type: SessionType | None = None
    class_start: str | None = None
    class_end: str | None = None


class TranscriptTurn(BaseModel):
    start: float
    end: float
    role: Literal["teacher", "kid", "media", "other"]
    text: str


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "t_start": {"type": "string", "description": "MM:SS or H:MM:SS from the start of the full recording"},
        "t_end": {"type": "string"},
        "modality": _enum(["visual", "audio", "both"]),
        "observation": {"type": "string", "description": "What was seen/heard. Never name or describe children."},
        "quote": {"type": "string", "description": "Short verbatim teacher quote, if any. Replace child names with [kid]."},
    },
    "required": ["t_start", "modality", "observation"],
}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "status": _enum([s.value for s in Status]),
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "rationale": {"type": "string"},
    },
    "required": ["status", "evidence", "rationale"],
}

_CAMERA_SCHEMA = {
    "type": "object",
    "properties": {
        "branded_background": _enum(["present", "absent", "unclear"]),
        "face_visible": _enum(["mostly", "sometimes", "rarely", "unclear"]),
        "camera_on_while_teaching": _enum(["mostly", "sometimes", "rarely", "unclear"]),
        "lighting": _enum(["ok", "dim", "unclear"]),
    },
    "required": ["branded_background", "face_visible", "camera_on_while_teaching", "lighting"],
}


def gemini_response_schema(call: str) -> dict:
    """JSON schema the Gemini observer must follow for `call` (main|opening|closing)."""
    item_ids = CALL_ITEMS[call]
    props: dict = {
        "items": {
            "type": "object",
            "properties": {i: _ITEM_SCHEMA for i in item_ids},
            "required": list(item_ids),
        },
        "camera": _CAMERA_SCHEMA,
    }
    required = ["items", "camera"]
    if call == "main":
        props["session_type"] = _enum(["regular_class", "demo_trial", "not_a_class", "unknown"])
        props["class_start"] = {"type": "string"}
        props["class_end"] = {"type": "string"}
        required += ["session_type", "class_start", "class_end"]
    return {"type": "object", "properties": props, "required": required}
