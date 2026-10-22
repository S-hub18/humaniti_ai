import pytest

from cqa.evidence import fmt_ts, parse_ts, validate
from cqa.rubric import Status
from cqa.schema import Evidence, ItemObservation, ObserverOutput, TranscriptTurn


def _out(items, window=(0.0, 3600.0)):
    return ObserverOutput(call="main", window=window, items=items)


def _met(t, quote=None):
    return ItemObservation(status=Status.MET, evidence=[Evidence(t_start=t, observation="x", quote=quote)], rationale="r")


def test_parse_ts_variants():
    assert parse_ts("05:07") == 307
    assert parse_ts("5:07") == 307
    assert parse_ts("75:30") == 4530
    assert parse_ts("1:15:30") == 4530
    assert parse_ts("00:00:21.5") == 21.5
    with pytest.raises(ValueError):
        parse_ts("soon")
    with pytest.raises(ValueError):
        parse_ts("12:75")


def test_fmt_ts():
    assert fmt_ts(307) == "05:07"
    assert fmt_ts(4530) == "1:15:30"


def test_drops_out_of_range_evidence():
    out, flags = validate(_out({"homework": _met("99:00")}), duration=3000.0)
    assert out.items["homework"].status == Status.NOT_OBSERVABLE
    assert "unsupported_claim:homework" in flags


def test_drops_outside_window():
    o = _out({"closing": _met("10:00")}, window=(2400.0, 3000.0))
    out, _ = validate(o, duration=3000.0)
    assert out.items["closing"].status == Status.NOT_OBSERVABLE
    # inside window (with 5s tolerance) kept
    o2 = _out({"closing": _met("39:57")}, window=(2400.0, 3000.0))
    out2, flags2 = validate(o2, duration=3000.0)
    assert out2.items["closing"].status == Status.MET and not flags2


def test_quote_must_match_transcript():
    tr = [TranscriptTurn(start=2710, end=2714, role="teacher", text="So this is your today's homework, okay?")]
    good = _out({"homework": _met("45:12", quote="This is your today's homework")})
    out, flags = validate(good, duration=3000.0, transcript=tr)
    assert out.items["homework"].status == Status.MET and not flags
    bad = _out({"homework": _met("45:12", quote="Please solve ten puzzles on lichess tonight")})
    out, flags = validate(bad, duration=3000.0, transcript=tr)
    assert out.items["homework"].status == Status.NOT_OBSERVABLE
    far = _out({"homework": _met("20:00", quote="This is your today's homework")})
    out, _ = validate(far, duration=3000.0, transcript=tr)
    assert out.items["homework"].status == Status.NOT_OBSERVABLE


def test_met_without_evidence_becomes_not_observable():
    o = _out({"rapport_intro": ItemObservation(status=Status.PARTIAL, evidence=[], rationale="r")})
    out, flags = validate(o, duration=3000.0)
    assert out.items["rapport_intro"].status == Status.NOT_OBSERVABLE
    assert flags == ["unsupported_claim:rapport_intro"]


def test_not_met_without_evidence_kept():
    o = _out({"homework": ItemObservation(status=Status.NOT_MET, evidence=[], rationale="no homework given")})
    out, flags = validate(o, duration=3000.0)
    assert out.items["homework"].status == Status.NOT_MET and not flags


def test_unparseable_timestamp_dropped():
    o = _out({"homework": _met("around the end")})
    out, flags = validate(o, duration=3000.0)
    assert out.items["homework"].status == Status.NOT_OBSERVABLE
