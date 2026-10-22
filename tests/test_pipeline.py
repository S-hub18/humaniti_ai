from cqa.explain import build_explanation
from cqa.media import MediaRef
from cqa.observer import MockObserver
from cqa.pipeline import PipelineConfig, assess
from cqa.rubric import ITEM_IDS, Grade, Status

MEDIA = MediaRef(path="x.mp4", sha256="abc", duration=3000.0)


class Recorder(MockObserver):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.samples = []

    def observe(self, media, spec, sample_idx):
        self.samples.append((spec.name, sample_idx))
        return super().observe(media, spec, sample_idx)


def test_pipeline_oracle_excellent():
    obs = MockObserver(oracle={i: Status.MET for i in ITEM_IDS})
    run = assess(MEDIA, obs, PipelineConfig())
    assert run.assessment.grade == Grade.EXCELLENT
    assert obs.calls == 9 and len(run.runs) == 3 and len(run.stats) == 9


def test_pipeline_unsupported_claim_flag_propagates():
    bad = {"call": "main", "items": {i: {"status": "met", "evidence": [{"t_start": "99:00", "observation": "o"}], "rationale": "r"} for i in ITEM_IDS},
           "camera": {"branded_background": "present", "face_visible": "mostly", "camera_on_while_teaching": "mostly", "lighting": "ok"},
           "session_type": "regular_class"}
    empty = {"items": {}}
    obs = MockObserver(script={"main": [bad], "opening": [empty], "closing": [empty]})
    run = assess(MEDIA, obs, PipelineConfig(k=1))
    assert "unsupported_claim:homework" in run.assessment.flags
    assert run.assessment.grade is None and run.assessment.abstain_reason == "insufficient_observations"


def test_pipeline_sample_offset_uses_distinct_samples():
    obs = Recorder(oracle={i: Status.MET for i in ITEM_IDS})
    assess(MEDIA, obs, PipelineConfig(k=3, sample_offset=3))
    assert sorted({s for _, s in obs.samples}) == [3, 4, 5]


def test_pipeline_module_votes_and_gates():
    from cqa.aggregate import ModuleVote
    from cqa.scorer import GateSignal

    obs = MockObserver(oracle={i: Status.MET for i in ITEM_IDS})
    run = assess(MEDIA, obs, PipelineConfig(k=1),
                 module_votes=[ModuleVote(module="b1", item="homework", status=Status.NOT_MET, weight=2.0)],
                 gates=[GateSignal(module="xc", field="face_visible", value="rarely", n_frames=30)])
    assert run.assessment.items["homework"].status == Status.NOT_MET
    assert run.assessment.grade == Grade.POOR and "compliance:face_not_visible" in run.assessment.flags


def test_every_bullet_has_timestamp_or_not_observed():
    oracle = {i: Status.MET for i in ITEM_IDS}
    oracle.update(homework=Status.NOT_MET, closing=Status.PARTIAL, new_joiner_intro=Status.NOT_APPLICABLE)
    a = assess(MEDIA, MockObserver(oracle=oracle), PipelineConfig()).assessment
    ex = build_explanation(a)
    bullets = ex["strengths"] + ex["improvements"]
    assert bullets and all(b.startswith("[") or b.startswith("Missing:") for b in bullets)
    assert not any("New joiners" in b for b in bullets)  # N/A item produces no bullet
    assert ex["headline"].startswith("Good") or ex["headline"].startswith("Excellent")


def test_demo_headline():
    obs = MockObserver(oracle={i: Status.MET for i in ITEM_IDS}, session_type="demo_trial")
    ex = build_explanation(assess(MEDIA, obs, PipelineConfig()).assessment)
    assert "demo" in ex["headline"].lower() and "not graded" in ex["headline"].lower()


# ---- flow-review fix 6: specific absences, not rubric template text ----
def test_not_met_states_the_specific_absence():
    from cqa.explain import ABSENCE

    oracle = {i: Status.MET for i in ITEM_IDS}
    oracle.update(homework=Status.NOT_MET, closing=Status.NOT_MET)
    ex = build_explanation(assess(MEDIA, MockObserver(oracle=oracle), PipelineConfig()).assessment)
    assert ABSENCE["homework"] in ex["improvements"]
    assert "no homework" in ABSENCE["homework"].lower() and "at any point" in ABSENCE["homework"]
    assert ABSENCE["closing"] in ex["improvements"]
    assert set(ABSENCE) == set(ITEM_IDS)
    assert not any(b.startswith("Not observed:") for b in ex["improvements"])
