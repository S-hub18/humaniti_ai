from cqa.rubric import (
    DEFAULT_CUTPOINTS,
    ITEM_IDS,
    ITEMS,
    STATUS_VALUE,
    WEIGHTS,
    Grade,
    Status,
)
from cqa.schema import (
    CameraObs,
    Evidence,
    ItemObservation,
    ObserverOutput,
    gemini_response_schema,
)


def test_weights_sum_100():
    assert sum(WEIGHTS.values()) == 100
    assert WEIGHTS == {
        "individual_calling": 20,
        "rapport_intro": 18,
        "energy_warmth": 18,
        "camera_background": 14,
        "homework": 12,
        "closing": 10,
        "new_joiner_intro": 8,
    }
    assert set(ITEM_IDS) == set(WEIGHTS) and len(ITEMS) == 7


def test_status_values():
    assert STATUS_VALUE[Status.MET] == 1.0
    assert STATUS_VALUE[Status.PARTIAL] == 0.5
    assert STATUS_VALUE[Status.NOT_MET] == 0.0
    assert STATUS_VALUE[Status.NOT_APPLICABLE] is None
    assert STATUS_VALUE[Status.NOT_OBSERVABLE] is None


def test_grade_labels():
    assert [g.label for g in Grade] == ["Poor", "Average", "Good", "Excellent"]
    assert DEFAULT_CUTPOINTS == (50.0, 68.0, 84.0)


def test_schema_roundtrip():
    out = ObserverOutput(
        call="main",
        window=(0.0, 3600.0),
        items={
            "homework": ItemObservation(
                status=Status.MET,
                evidence=[Evidence(t_start="45:10", observation="assigns puzzle as homework", quote="This is your today's homework")],
                rationale="explicit assignment",
            )
        },
        camera=CameraObs(branded_background="present"),
        session_type="regular_class",
        class_start="00:21",
        class_end="55:58",
    )
    again = ObserverOutput.model_validate_json(out.model_dump_json())
    assert again == out


def test_gemini_schema_items_per_call():
    opening = gemini_response_schema("opening")
    assert set(opening["properties"]["items"]["properties"]) == {
        "rapport_intro",
        "new_joiner_intro",
        "camera_background",
    }
    assert "session_type" not in opening["properties"]
    main = gemini_response_schema("main")
    assert set(main["properties"]["items"]["properties"]) == set(ITEM_IDS)
    assert "session_type" in main["properties"]
    closing = gemini_response_schema("closing")
    assert set(closing["properties"]["items"]["properties"]) == {"closing", "homework", "camera_background"}
