"""Versioned observer prompts. Bump PROMPT_VERSION on any wording change (it is part of the cache key)."""

from __future__ import annotations

from cqa.evidence import fmt_ts
from cqa.observer import CallSpec
from cqa.rubric import CALL_ITEMS, ITEM_BY_ID

PROMPT_VERSION = "v1"

PRIMER = """You are an experienced quality reviewer for 8x8 (Eight Times Eight), an online chess academy for children.
You are watching AND listening to a recorded Zoom class. You OBSERVE and REPORT evidence against a checklist; you do NOT grade the class.

What these recordings look like:
- Most of the screen is the teacher's screen share (lichess / ChessKid / chess.com boards, puzzles, arena tournaments, lesson videos).
- A small active-speaker tile (usually top-right) shows whoever is talking: often the teacher, often a child.
- Teachers are expected to use the branded "Eight Times Eight" / 8x8 virtual background.
- Long quiet stretches while children play an arena/puzzle on lichess are a legitimate activity, not dead air.
- Played lesson videos (another narrator's voice, a video player on screen) are NOT the teacher speaking.

Rules:
- Cite timestamps as MM:SS (or H:MM:SS) measured from the START OF THE FULL RECORDING, not from the start of the clip.
- Every 'met' or 'partial' status needs at least one evidence item with a timestamp; quotes must be short and verbatim.
- Never name or describe children. Replace any child's name in quotes with [kid]. Do not describe children's faces, clothes or homes.
- Use 'not_observable' when you cannot tell, and 'not_applicable' only where the checklist allows it.
- Judge only what is in the recording."""

MAIN_EXTRA = """Also report:
- session_type: 'regular_class' (a batch class with several children), 'demo_trial' (a one-to-one trial/demo, typically a parent present, age/onboarding/scheduling/enrolment talk), 'not_a_class', or 'unknown'.
- class_start / class_end: when teaching actually starts and ends (ignore waiting time before and silent recording after)."""

CAMERA_EXTRA = """camera: report the teacher's camera/background state across this segment:
branded_background (present/absent/unclear), face_visible (mostly/sometimes/rarely/unclear),
camera_on_while_teaching (mostly/sometimes/rarely/unclear), lighting (ok/dim/unclear)."""


def _item_block(item_id: str) -> str:
    d = ITEM_BY_ID[item_id]
    anchors = "\n".join(f"    - {k}: {v}" for k, v in d.anchors.items())
    return f"- {item_id} ({d.title}): {d.description}\n{anchors}"


def build_prompt(spec: CallSpec, duration: float) -> str:
    if spec.name == "main":
        scope = f"You are given the FULL recording ({fmt_ts(duration)} long)."
    else:
        part = "OPENING" if spec.name == "opening" else "CLOSING"
        scope = (
            f"You are given the {part} segment only: {fmt_ts(spec.start)} to {fmt_ts(spec.end)} of a "
            f"{fmt_ts(duration)} recording, at higher visual detail. Timestamps must still be from the start of the full recording."
        )
    items = "\n".join(_item_block(i) for i in CALL_ITEMS[spec.name])
    parts = [PRIMER, scope, "Checklist items for this segment:", items, CAMERA_EXTRA]
    if spec.name == "main":
        parts.append(MAIN_EXTRA)
    parts.append("Respond with JSON matching the provided schema only.")
    return "\n\n".join(parts)
