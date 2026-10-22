from pathlib import Path

from cqa.aggregate import ModuleVote
from cqa.media import MediaRef
from cqa.observer import MockObserver
from cqa.rubric import ITEM_IDS, Status
from eval.run_eval import EvalConfig, ablate, load_dataset, run_eval, write_report

ROOT = Path(__file__).resolve().parents[1]
ORACLE_BY_ORD = {
    3: {i: Status.MET for i in ITEM_IDS},
    2: {**{i: Status.MET for i in ITEM_IDS}, "homework": Status.NOT_MET, "closing": Status.PARTIAL},
    1: {**{i: Status.PARTIAL for i in ITEM_IDS}, "individual_calling": Status.MET},
    0: {**{i: Status.NOT_MET for i in ITEM_IDS}, "individual_calling": Status.PARTIAL},
}


class CountingFactory:
    def __init__(self):
        self.observers = []

    def __call__(self, entry):
        ordinal = entry["ordinal"]
        oracle = ORACLE_BY_ORD[ordinal if ordinal is not None else 2]
        obs = MockObserver(oracle=oracle, noise=0.1, seed=hash(entry["id"]) % 1000,
                           session_type="demo_trial" if entry["label"] == "demo" else "regular_class")
        self.observers.append(obs)
        return obs


def fake_media(entry):
    return MediaRef(path=f"{entry['id']}.mp4", sha256=entry["id"], duration=3000.0)


def _run(tmp_path, **kw):
    factory = CountingFactory()
    cfg = EvalConfig(samples=6, k=3, include_holdout=kw.get("include_holdout", False))
    result = run_eval(load_dataset(ROOT / "eval" / "dataset.yaml"), factory, cfg, media_resolver=fake_media)
    return result, factory


def test_runner_mock_end_to_end(tmp_path):
    result, factory = _run(tmp_path)
    ids = [c["id"] for c in result["classes"]]
    assert "B552" not in ids and "B482" not in ids  # holdouts excluded by default
    assert "DemoEN" in ids
    m = result["metrics"]
    assert m["n"] == 8 and 0.0 <= m["exact"] <= 1.0 and m["pairwise_n"] > 0
    assert "baseline_exact" in m and "loo" in result
    assert "flip_rate" in result["variance"]
    report = write_report(result, tmp_path)
    text = report.read_text()
    for header in ("## Grades", "## Metrics", "## LOO cutpoints", "## Run variance", "## Item accuracy",
                   "## Cost and latency", "## Ablation", "## Out-of-distribution (demos)"):
        assert header in text
    assert "Grade-level accuracy rests on 10 real labels" in text
    assert "MOCK" in text  # mock runs are labelled as such
    demo = next(c for c in result["classes"] if c["id"] == "DemoEN")
    assert demo["grade"] is None and demo["session_type"] == "demo"


def test_include_holdout(tmp_path):
    result, _ = _run(tmp_path, include_holdout=True)
    assert result["metrics"]["n"] == 10


def test_ablation_no_observer_calls(tmp_path):
    result, factory = _run(tmp_path)
    calls_before = sum(o.calls for o in factory.observers)
    votes = {"B472": [ModuleVote(module="b1", item="homework", status=Status.NOT_MET, weight=3.0)]}
    rows = ablate(result["_runs"], result["_labels"], votes, {}, {"core": set(), "core+b1": {"b1"}})
    assert sum(o.calls for o in factory.observers) == calls_before
    names = [r["config"] for r in rows]
    assert names == ["core", "core+b1"]
    assert all({"exact", "within1", "mae", "spearman", "pairwise"} <= set(r) for r in rows)
