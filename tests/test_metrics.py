import csv
from pathlib import Path

import pytest
import yaml

from cqa.rubric import ITEM_IDS, Status
from eval.metrics import (
    exact_acc,
    item_accuracy,
    mae,
    majority_baseline,
    pairwise_acc,
    spearman,
    within1_acc,
)

ROOT = Path(__file__).resolve().parents[1]


def test_metrics_known_values():
    y = [3, 2, 1, 0]
    p = [3, 1, 1, 2]
    assert exact_acc(y, p) == 0.5
    assert within1_acc(y, p) == 0.75
    assert mae(y, p) == pytest.approx(0.75)
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_pairwise_counts_36_for_label_distribution():
    y = [3, 3, 2, 2, 1, 1, 0, 0, 0, 0]
    acc, n = pairwise_acc(y, [float(v) for v in y])
    assert n == 36 and acc == 1.0
    acc_tied, _ = pairwise_acc(y, [5.0] * 10)
    assert acc_tied == 0.5


def test_majority_baseline():
    assert majority_baseline([3, 3, 2, 2, 1, 1, 0, 0, 0, 0]) == [0] * 10


def test_item_accuracy_lenient():
    gt = [
        {"class": "A", "item": "homework", "status": "met"},
        {"class": "B", "item": "homework", "status": "not_met"},
        {"class": "C", "item": "energy_warmth", "status": "partial"},
    ]
    pred = {
        "A": {"homework": Status.PARTIAL},
        "B": {"homework": Status.NOT_MET},
        "C": {"energy_warmth": Status.NOT_MET},
    }
    strict = item_accuracy(gt, pred, lenient=False)
    assert strict["homework"] == (0.5, 2) and strict["energy_warmth"] == (0.0, 1)
    lenient = item_accuracy(gt, pred, lenient=True)
    assert lenient["homework"] == (1.0, 2) and lenient["energy_warmth"] == (1.0, 1)


def test_item_accuracy_null_prediction_counts_wrong():
    gt = [{"class": "A", "item": "homework", "status": "met"}]
    assert item_accuracy(gt, {"A": {"homework": Status.NOT_OBSERVABLE}}, lenient=True)["homework"] == (0.0, 1)


def test_dataset_yaml_valid():
    ds = yaml.safe_load((ROOT / "eval" / "dataset.yaml").read_text())
    classes = ds["classes"]
    assert len(classes) == 12
    assert sorted(c["id"] for c in classes if c["holdout"]) == ["B482", "B552"]
    demos = [c for c in classes if c["label"] == "demo"]
    assert len(demos) == 2 and all(c["ordinal"] is None for c in demos)
    assert sorted(c["ordinal"] for c in classes if c["ordinal"] is not None) == [0, 0, 0, 0, 1, 1, 2, 2, 3, 3]


def test_ground_truth_items_valid():
    rows = list(csv.DictReader((ROOT / "eval" / "ground_truth_items.csv").open()))
    ids = {c["id"] for c in yaml.safe_load((ROOT / "eval" / "dataset.yaml").read_text())["classes"]}
    assert rows and all(r["source"] == "grader" for r in rows)
    assert all(r["item"] in ITEM_IDS and r["class"] in ids for r in rows)
    assert all(r["status"] in ("met", "partial", "not_met") for r in rows)
    assert len({(r["class"], r["item"]) for r in rows}) == len(rows)


def test_silver_complete():
    rows = list(csv.DictReader((ROOT / "eval" / "silver_items.csv").open()))
    assert len(rows) == 70 and all(r["source"] == "ours" for r in rows)
    assert {(r["class"], r["item"]) for r in rows} == {(c, i) for c in {r["class"] for r in rows} for i in ITEM_IDS}
    assert all(r["status"] in ("met", "partial", "not_met", "not_applicable", "not_observable") for r in rows)
    assert not any('"' in r["evidence"] for r in rows)  # repo copy holds timestamps only, no quotes
