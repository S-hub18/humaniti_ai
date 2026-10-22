"""Eval metrics. Grade-level numbers rest on 10 real labels; report n alongside every number."""

from __future__ import annotations

from collections import Counter
from itertools import combinations

import numpy as np

from cqa.rubric import ORDINAL, Status


def exact_acc(y: list[int], p: list[int]) -> float:
    return sum(a == b for a, b in zip(y, p)) / len(y)


def within1_acc(y: list[int], p: list[int]) -> float:
    return sum(abs(a - b) <= 1 for a, b in zip(y, p)) / len(y)


def mae(y: list[int], p: list[int]) -> float:
    return sum(abs(a - b) for a, b in zip(y, p)) / len(y)


def _ranks(x: list[float]) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    order = x.argsort(kind="mergesort")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x), dtype=float)
    for v in np.unique(x):  # average ranks for ties
        idx = x == v
        ranks[idx] = ranks[idx].mean()
    return ranks


def spearman(y: list[float], s: list[float]) -> float:
    ry, rs = _ranks(y), _ranks(s)
    if ry.std() == 0 or rs.std() == 0:
        return float("nan")
    return float(np.corrcoef(ry, rs)[0, 1])


def pairwise_acc(y: list[int], s: list[float]) -> tuple[float, int]:
    """Ordering accuracy over pairs with different labels; tied predictions count 0.5."""
    hits, n = 0.0, 0
    for i, j in combinations(range(len(y)), 2):
        if y[i] == y[j]:
            continue
        n += 1
        if s[i] == s[j]:
            hits += 0.5
        elif (s[i] > s[j]) == (y[i] > y[j]):
            hits += 1
    return (hits / n if n else float("nan")), n


def majority_baseline(y: list[int]) -> list[int]:
    counts = Counter(y)
    top = max(counts.values())
    label = min(v for v, c in counts.items() if c == top)
    return [label] * len(y)


def _item_correct(gt: str, pred: Status, lenient: bool) -> bool:
    if pred not in ORDINAL:  # not_applicable / not_observable never match a graded label
        return False
    g, p = ORDINAL[Status(gt)], ORDINAL[pred]
    return abs(g - p) <= 1 if lenient else g == p


def item_accuracy(
    gt_rows: list[dict],
    pred: dict[str, dict[str, Status]],
    lenient: bool,
) -> dict[str, tuple[float, int]]:
    """Per-item accuracy vs labelled rows ({class,item,status}); returns {item: (acc, n)}."""
    hits: Counter = Counter()
    totals: Counter = Counter()
    for r in gt_rows:
        p = pred.get(r["class"], {}).get(r["item"])
        if p is None:
            continue
        totals[r["item"]] += 1
        hits[r["item"]] += _item_correct(r["status"], Status(p), lenient)
    return {i: (hits[i] / totals[i], totals[i]) for i in totals}
