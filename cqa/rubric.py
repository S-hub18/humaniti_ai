"""Rubric checklist derived from the grader feedback (feedbacks.pdf).

Weights are fixed priors (spec §3) and must never be fitted to labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


class Status(str, Enum):
    MET = "met"
    PARTIAL = "partial"
    NOT_MET = "not_met"
    NOT_APPLICABLE = "not_applicable"
    NOT_OBSERVABLE = "not_observable"


STATUS_VALUE: dict[Status, float | None] = {
    Status.MET: 1.0,
    Status.PARTIAL: 0.5,
    Status.NOT_MET: 0.0,
    Status.NOT_APPLICABLE: None,
    Status.NOT_OBSERVABLE: None,
}

# Ordinal position for statuses that carry a value (used for medians / "most positive").
ORDINAL: dict[Status, int] = {Status.NOT_MET: 0, Status.PARTIAL: 1, Status.MET: 2}


class Grade(IntEnum):
    POOR = 0
    AVERAGE = 1
    GOOD = 2
    EXCELLENT = 3

    @property
    def label(self) -> str:
        return self.name.capitalize()


DEFAULT_CUTPOINTS: tuple[float, float, float] = (50.0, 68.0, 84.0)


@dataclass(frozen=True)
class ItemDef:
    id: str
    title: str
    weight: int
    description: str
    anchors: dict[str, str] = field(default_factory=dict)


ITEMS: tuple[ItemDef, ...] = (
    ItemDef(
        "individual_calling",
        "Individual calling & inclusion",
        20,
        "Teacher calls on children individually by name to answer or play, and includes every child, not just the loudest one.",
        {
            "met": "Most children are addressed by name and given turns; teacher rotates so everyone participates.",
            "partial": "Some children are called individually, but one or two dominate or several are never addressed.",
            "not_met": "Questions are thrown to the group; children are rarely addressed individually.",
        },
    ),
    ItemDef(
        "rapport_intro",
        "Rapport & introduction",
        18,
        "Warm opening: greets children individually, small talk (e.g. how was your day), then introduces the session's topic.",
        {
            "met": "Individual greetings plus personal small talk before a clear introduction of the topic.",
            "partial": "Brief generic greeting, or a topic intro without personal rapport.",
            "not_met": "Goes directly into revision/teaching without greeting or rapport building.",
        },
    ),
    ItemDef(
        "energy_warmth",
        "Energy & warmth",
        18,
        "Energetic, pleasant, happy delivery: lively voice, praise and encouragement, smiling, positive reactions from children.",
        {
            "met": "Consistently lively, encouraging tone with frequent specific praise; children respond positively.",
            "partial": "Polite but flat or uneven energy; praise is rare or mechanical.",
            "not_met": "Monotone, curt or impatient delivery; little encouragement.",
        },
    ),
    ItemDef(
        "camera_background",
        "Camera, face & 8x8 background",
        14,
        "Teacher's camera is on while teaching, face clearly visible and well lit, using the branded 8x8 / Eight Times Eight virtual background.",
        {
            "met": "Camera on throughout, face visible and lit, branded background present.",
            "partial": "One problem: camera off at times, dim lighting, or noisy background cutout.",
            "not_met": "No branded background, or face not visible for most of the class.",
        },
    ),
    ItemDef(
        "homework",
        "Homework",
        12,
        "Teacher assigns homework or practice (puzzles, links) for after the class. Can happen anywhere in the class.",
        {
            "met": "Explicit homework/practice assignment with what to do.",
            "partial": "Vague suggestion to practise, or only checks previous homework without setting new work.",
            "not_met": "No homework or practice assigned.",
        },
    ),
    ItemDef(
        "closing",
        "Conclusion",
        10,
        "Proper conclusion: recap of what was learned, preview of next class, warm goodbye.",
        {
            "met": "Recap of the lesson plus a warm goodbye (and/or next-class preview).",
            "partial": "Goodbye only, or a rushed wrap-up.",
            "not_met": "Class ends abruptly, or teacher leaves without a conclusion.",
        },
    ),
    ItemDef(
        "new_joiner_intro",
        "New joiners introduced",
        8,
        "When a new child joins the batch, the teacher gives them time to introduce themselves and welcomes them.",
        {
            "met": "New child is asked to self-introduce and is welcomed by the group.",
            "partial": "New child acknowledged but not given time to introduce themselves.",
            "not_met": "A new child is present but ignored for introductions.",
            "not_applicable": "No new child joins in this class.",
        },
    ),
)

ITEM_IDS: tuple[str, ...] = tuple(i.id for i in ITEMS)
WEIGHTS: dict[str, int] = {i.id: i.weight for i in ITEMS}
ITEM_BY_ID: dict[str, ItemDef] = {i.id: i for i in ITEMS}

# Items each Gemini call is asked about (spec §2).
CALL_ITEMS: dict[str, tuple[str, ...]] = {
    "main": ITEM_IDS,
    "opening": ("rapport_intro", "new_joiner_intro", "camera_background"),
    "closing": ("closing", "homework", "camera_background"),
}
