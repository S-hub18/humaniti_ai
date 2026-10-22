"""Template explanation built only from validated evidence (spec §5.7)."""

from __future__ import annotations

from cqa.rubric import ITEMS, STATUS_VALUE, Status
from cqa.scorer import Assessment

MAX_EVIDENCE_PER_ITEM = 2

FLAG_TEXT = {
    "compliance:no_branded_background": "Compliance: the 8x8 branded background was not used (grade capped at Poor).",
    "compliance:face_not_visible": "Compliance: the teacher's face was rarely visible (grade capped at Poor).",
    "improve:camera_off_at_times": "Teacher camera was off at times while teaching.",
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
        if ic.evidence:
            for ev in ic.evidence[:MAX_EVIDENCE_PER_ITEM]:
                quote = f' "{ev.quote}"' if ev.quote else ""
                target.append(f"[{ev.t_start}] {label}: {ev.observation}{quote}")
        else:
            target.append(f"Not observed: {label}: {item.anchors.get(ic.status.value, item.description)}")
    return {
        "headline": _headline(a),
        "strengths": strengths,
        "improvements": improvements,
        "flags": [_flag_text(f) for f in a.flags],
    }
