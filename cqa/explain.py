"""Template explanation built only from validated evidence (spec §5.7)."""

from __future__ import annotations

from cqa.rubric import ITEMS, STATUS_VALUE, Status
from cqa.scorer import Assessment

MAX_EVIDENCE_PER_ITEM = 2

# Specific absences for not_met items (said plainly, instead of rubric template text).
ABSENCE = {
    "individual_calling": "Missing: children were rarely called on individually by name; questions went to the whole group.",
    "rapport_intro": "Missing: the class started without individual greetings or small talk before the lesson.",
    "energy_warmth": "Missing: delivery stayed flat, with little praise or encouragement for the children.",
    "camera_background": "Missing: the teacher's camera, face or branded 8x8 background was absent for much of the class.",
    "homework": "Missing: no homework or practice was given at any point in the class.",
    "closing": "Missing: the class ended without a recap or a proper goodbye.",
    "new_joiner_intro": "Missing: a new child joined but was not given time to introduce themselves.",
}

FLAG_TEXT = {
    "compliance:no_branded_background": "Compliance: the 8x8 branded background was not used (grade capped at Poor).",
    "compliance:face_not_visible": "Compliance: the teacher's face was rarely visible (grade capped at Poor).",
    "improve:camera_off_at_times": "Teacher camera was off at times while teaching.",
    "cap:two_not_met": "Two checklist items were clearly not met, so the grade is capped at Average.",
    "cap:three_or_more_not_met": "Three or more checklist items were clearly not met, so the grade is capped at Poor.",
}


def _flag_text(flag: str) -> str:
    if flag in FLAG_TEXT:
        return FLAG_TEXT[flag]
    if flag.startswith("crosscheck_disagree:"):
        return f"Local check disagreed with the AI on {flag.split(':', 1)[1].replace('_', ' ')}; treated conservatively."
    if flag.startswith("unsupported_claim:"):
        return f"An AI claim about {flag.split(':', 1)[1].replace('_', ' ')} could not be verified and was ignored."
    return flag


def _headline(a: Assessment) -> str:
    if a.session_type == "demo":
        return "Demo / trial session detected: not graded as a regular class."
    if a.grade is None:
        return f"Not graded: {(a.abstain_reason or 'unknown').replace('_', ' ')}."
    return f"{a.grade.label} (score {a.score:.0f}/100, confidence {a.confidence})"


def build_explanation(a: Assessment) -> dict:
    strengths: list[str] = []
    improvements: list[str] = []
    for item in ITEMS:
        ic = a.items.get(item.id)
        if ic is None or STATUS_VALUE[ic.status] is None:
            continue
        target = strengths if ic.status == Status.MET else improvements
        label = item.title if ic.status == Status.MET else f"{item.title} ({ic.status.value.replace('_', ' ')})"
        if ic.status == Status.NOT_MET:
            target.append(ABSENCE[item.id])
        if ic.evidence:
            for ev in ic.evidence[:MAX_EVIDENCE_PER_ITEM]:
                quote = f' "{ev.quote}"' if ev.quote else ""
                target.append(f"[{ev.t_start}] {label}: {ev.observation}{quote}")
        elif ic.status != Status.NOT_MET:
            target.append(f"Missing: {label}: no supporting moment was found in the recording.")
    return {
        "headline": _headline(a),
        "strengths": strengths,
        "improvements": improvements,
        "flags": [_flag_text(f) for f in a.flags],
    }
