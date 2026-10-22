from cqa.aggregate import MergedRun, ModuleVote, merge_calls, vote
from cqa.rubric import ITEM_IDS, Status
from cqa.schema import CameraObs, Evidence, ItemObservation, ObserverOutput

S = Status


def io(status, t="01:00"):
    ev = [Evidence(t_start=t, observation="obs")] if status in (S.MET, S.PARTIAL) else []
    return ItemObservation(status=status, evidence=ev, rationale="r")


def out(call, items, camera=None, session="regular_class"):
    return ObserverOutput(
        call=call,
        window=(0.0, 3600.0),
        items=items,
        camera=camera,
        session_type=session if call == "main" else None,
    )


def full_main(status=S.MET, **over):
    items = {i: io(status) for i in ITEM_IDS}
    items.update(over)
    return out("main", items, camera=CameraObs(branded_background="present", face_visible="mostly", camera_on_while_teaching="mostly", lighting="ok"))


def run(**statuses):
    items = {i: io(statuses.get(i, S.MET)) for i in ITEM_IDS}
    return MergedRun(items=items, camera=CameraObs(branded_background="present", face_visible="mostly"), session_type="regular_class")


def test_merge_opening_overrides_main_for_rapport():
    main = full_main(rapport_intro=io(S.NOT_MET))
    opening = out("opening", {"rapport_intro": io(S.MET), "new_joiner_intro": io(S.NOT_APPLICABLE), "camera_background": io(S.MET)})
    m = merge_calls(main, opening, None)
    assert m.items["rapport_intro"].status == S.MET
    assert m.items["new_joiner_intro"].status == S.NOT_APPLICABLE


def test_merge_opening_null_falls_back_to_main():
    main = full_main(rapport_intro=io(S.PARTIAL))
    opening = out("opening", {"rapport_intro": io(S.NOT_OBSERVABLE)})
    assert merge_calls(main, opening, None).items["rapport_intro"].status == S.PARTIAL


def test_merge_homework_agreeing_calls_and_closing_override():
    main = full_main(homework=io(S.MET))
    closing = out("closing", {"homework": io(S.MET, "50:00"), "closing": io(S.PARTIAL)})
    m = merge_calls(main, None, closing)
    assert m.items["homework"].status == S.MET
    assert m.items["closing"].status == S.PARTIAL


def test_merge_camera_conservative():
    main = full_main()
    opening = out("opening", {}, camera=CameraObs(branded_background="absent", face_visible="unclear", lighting="dim"))
    closing = out("closing", {}, camera=CameraObs(branded_background="present", face_visible="sometimes", lighting="ok"))
    m = merge_calls(main, opening, closing)
    assert m.camera.branded_background == "absent"
    assert m.camera.face_visible == "sometimes"  # unclear ignored
    assert m.camera.lighting == "dim"


def test_missing_item_becomes_not_observable():
    main = out("main", {"homework": io(S.MET), "bogus_item": io(S.MET)})
    m = merge_calls(main, None, None)
    assert set(m.items) == set(ITEM_IDS)
    assert m.items["closing"].status == S.NOT_OBSERVABLE
    assert "bogus_item" not in m.items


def test_vote_majority_and_agreement():
    c = vote([run(homework=S.MET), run(homework=S.MET), run(homework=S.PARTIAL)])
    assert c.items["homework"].status == S.MET
    assert abs(c.items["homework"].agreement - 2 / 3) < 1e-9
    assert c.items["closing"].agreement == 1.0


def test_three_way_tie_takes_median():
    c = vote([run(homework=S.MET), run(homework=S.PARTIAL), run(homework=S.NOT_MET)])
    assert c.items["homework"].status == S.PARTIAL
    assert abs(c.items["homework"].agreement - 1 / 3) < 1e-9


def test_null_needs_strict_majority():
    c = vote([run(closing=S.NOT_OBSERVABLE), run(closing=S.MET), run(closing=S.NOT_MET)])
    assert c.items["closing"].status in (S.MET, S.NOT_MET, S.PARTIAL)
    c2 = vote([run(closing=S.NOT_OBSERVABLE), run(closing=S.NOT_OBSERVABLE), run(closing=S.MET)])
    assert c2.items["closing"].status == S.NOT_OBSERVABLE


def test_module_vote_breaks_tie():
    runs = [run(homework=S.MET), run(homework=S.NOT_MET)]
    c = vote(runs, extra=[ModuleVote(module="b1", item="homework", status=S.NOT_MET)])
    assert c.items["homework"].status == S.NOT_MET


def test_session_type_mode():
    r1, r2, r3 = run(), run(), run()
    r3 = r3.model_copy(update={"session_type": "demo_trial"})
    c = vote([r1, r2, r3])
    assert c.session_type == "regular_class"
    assert abs(c.session_agreement - 2 / 3) < 1e-9


def test_evidence_deduplicated_within_10s():
    a = run()
    b = run()
    b.items["homework"] = io(S.MET, "01:05")
    c = vote([a, b])
    assert len(c.items["homework"].evidence) == 1


# ---- flow-review fix 5: stricter call merge ----
def test_focus_not_applicable_cannot_override_main_not_met():
    main = full_main(new_joiner_intro=io(S.NOT_MET), closing=io(S.NOT_MET))
    opening = out("opening", {"new_joiner_intro": io(S.NOT_APPLICABLE)})
    closing = out("closing", {"closing": io(S.NOT_APPLICABLE)})
    m = merge_calls(main, opening, closing)
    assert m.items["new_joiner_intro"].status == S.NOT_MET
    assert m.items["closing"].status == S.NOT_MET


def test_focus_not_applicable_still_used_when_main_not_negative():
    main = full_main(new_joiner_intro=io(S.NOT_OBSERVABLE))
    opening = out("opening", {"new_joiner_intro": io(S.NOT_APPLICABLE)})
    assert merge_calls(main, opening, None).items["new_joiner_intro"].status == S.NOT_APPLICABLE


def test_homework_needs_majority_tie_goes_conservative():
    main = full_main(homework=io(S.NOT_MET))
    closing = out("closing", {"homework": io(S.MET, "50:00")})
    assert merge_calls(main, None, closing).items["homework"].status == S.NOT_MET  # 1-1 tie -> stricter
    opening = out("opening", {"homework": io(S.MET, "05:00")})
    assert merge_calls(main, opening, closing).items["homework"].status == S.MET  # 2-1 majority
