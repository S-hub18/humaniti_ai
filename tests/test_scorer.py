import pytest

from cqa.aggregate import Consensus, ItemConsensus
from cqa.rubric import ITEM_IDS, Grade, Status
from cqa.schema import CameraObs, Evidence
from cqa.scorer import GateSignal, ScoringConfig, grade_from_score, score

S = Status
GOOD_CAM = CameraObs(branded_background="present", face_visible="mostly", camera_on_while_teaching="mostly", lighting="ok")


def cons(default=S.MET, agreement=1.0, camera=GOOD_CAM, session="regular_class", session_agreement=1.0, **over):
    items = {}
    for i in ITEM_IDS:
        st = over.get(i, default)
        ev = [Evidence(t_start="01:00", observation="o")] if st in (S.MET, S.PARTIAL) else []
        items[i] = ItemConsensus(status=st, agreement=agreement, evidence=ev, votes=[st] * 3)
    return Consensus(items=items, camera=camera, session_type=session, session_agreement=session_agreement)


def test_all_met_is_excellent():
    a = score(cons())
    assert a.grade == Grade.EXCELLENT and a.score == 100.0 and a.coverage == 1.0


def test_grade_boundaries():
    cp = (50.0, 68.0, 84.0)
    assert grade_from_score(49.9, cp) == Grade.POOR
    assert grade_from_score(50.0, cp) == Grade.AVERAGE
    assert grade_from_score(67.9, cp) == Grade.AVERAGE
    assert grade_from_score(68.0, cp) == Grade.GOOD
    assert grade_from_score(83.99, cp) == Grade.GOOD
    assert grade_from_score(84.0, cp) == Grade.EXCELLENT


def test_weighted_score_value():
    # homework (12) not_met, closing (10) partial -> (100 - 12 - 5) = 83 -> GOOD
    a = score(cons(homework=S.NOT_MET, closing=S.PARTIAL))
    assert a.score == pytest.approx(83.0)
    assert a.grade == Grade.GOOD


def test_null_items_renormalised():
    a = score(cons(new_joiner_intro=S.NOT_APPLICABLE))
    assert a.score == 100.0 and a.coverage == pytest.approx(0.92)


def test_coverage_abstain():
    a = score(cons(default=S.NOT_OBSERVABLE, individual_calling=S.MET, rapport_intro=S.MET))
    assert a.grade is None and a.abstain_reason == "insufficient_observations"
    assert a.score == pytest.approx(69.0)  # 38 met + 62 unobserved at the neutral prior 0.5


def test_no_background_caps_poor():
    cam = GOOD_CAM.model_copy(update={"branded_background": "absent"})
    a = score(cons(camera=cam))
    assert a.grade == Grade.POOR
    assert "compliance:no_branded_background" in a.flags


def test_face_rarely_caps_poor():
    cam = GOOD_CAM.model_copy(update={"face_visible": "rarely"})
    a = score(cons(camera=cam))
    assert a.grade == Grade.POOR and "compliance:face_not_visible" in a.flags


def test_camera_sometimes_flag_only():
    cam = GOOD_CAM.model_copy(update={"camera_on_while_teaching": "sometimes"})
    a = score(cons(camera=cam))
    assert a.grade == Grade.EXCELLENT and "improve:camera_off_at_times" in a.flags


def test_gate_never_raises_grade():
    cam = GOOD_CAM.model_copy(update={"branded_background": "absent"})
    a = score(cons(default=S.NOT_MET, camera=cam))
    assert a.grade == Grade.POOR


def test_demo_never_graded():
    a = score(cons(session="demo_trial"))
    assert a.grade is None and a.session_type == "demo" and a.abstain_reason is None


def test_session_unclear_abstains():
    a = score(cons(session="not_a_class"))
    assert a.grade is None and a.abstain_reason == "not_a_class"
    b = score(cons(session_agreement=0.5))
    assert b.grade is None and b.abstain_reason == "session_unclear"


def test_xc_gate_conservative_only():
    a = score(cons(), gates=[GateSignal(module="xc", field="branded_background", value="absent", n_frames=20)])
    assert a.grade == Grade.POOR
    assert "crosscheck_disagree:branded_background" in a.flags
    absent_cam = GOOD_CAM.model_copy(update={"branded_background": "absent"})
    b = score(cons(camera=absent_cam), gates=[GateSignal(module="xc", field="branded_background", value="present", n_frames=20)])
    assert b.grade == Grade.POOR and "crosscheck_disagree:branded_background" in b.flags
    c = score(cons(), gates=[GateSignal(module="xc", field="branded_background", value="absent", n_frames=5)])
    assert c.grade == Grade.EXCELLENT and not any(f.startswith("crosscheck") for f in c.flags)


def test_confidence_levels():
    assert score(cons()).confidence == "high"
    assert score(cons(agreement=0.6)).confidence == "low"
    assert score(cons(agreement=0.8)).confidence == "medium"
    # near a cutpoint (83 vs 84) -> not high
    assert score(cons(homework=S.NOT_MET, closing=S.PARTIAL)).confidence == "medium"
    unclear = GOOD_CAM.model_copy(update={"branded_background": "unclear"})
    assert score(cons(camera=unclear)).confidence == "low"


def test_custom_cutpoints():
    a = score(cons(homework=S.NOT_MET, closing=S.PARTIAL), ScoringConfig(cutpoints=(50.0, 68.0, 80.0)))
    assert a.grade == Grade.EXCELLENT


# ---- flow-review fix 3: dropped / unobserved items must not raise the score ----
def test_unobserved_items_score_at_neutral_prior_not_vanish():
    from cqa.scorer import NEUTRAL_PRIOR

    assert NEUTRAL_PRIOR == 0.5
    # a partial item that becomes not_observable must not raise the score
    with_partial = score(cons(rapport_intro=S.PARTIAL, homework=S.NOT_MET))
    dropped = score(cons(rapport_intro=S.NOT_OBSERVABLE, homework=S.NOT_MET))
    assert dropped.score <= with_partial.score
    assert dropped.coverage < with_partial.coverage


def test_dropping_a_met_claim_lowers_the_score():
    assert score(cons(homework=S.NOT_OBSERVABLE)).score < score(cons()).score


def test_min_graded_items_abstains():
    from cqa.scorer import MIN_GRADED_ITEMS

    assert MIN_GRADED_ITEMS == 5
    four = cons(default=S.NOT_OBSERVABLE, individual_calling=S.MET, rapport_intro=S.MET,
                energy_warmth=S.MET, camera_background=S.MET)  # 70/100 coverage but only 4 items
    a = score(four)
    assert a.grade is None and a.abstain_reason == "insufficient_observations"
    five = cons(default=S.NOT_OBSERVABLE, individual_calling=S.MET, rapport_intro=S.MET,
                energy_warmth=S.MET, camera_background=S.MET, homework=S.MET)
    assert score(five).grade is not None


def test_not_applicable_only_counts_for_new_joiners():
    na_new = score(cons(new_joiner_intro=S.NOT_APPLICABLE))
    na_homework = score(cons(homework=S.NOT_APPLICABLE))
    assert na_new.score == 100.0
    assert na_homework.score < 100.0  # treated as unobserved (neutral prior), not removed
