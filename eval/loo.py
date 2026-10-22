"""Leave-one-out cutpoint tuning (spec §5.5). Only the 3 cutpoints move, each within prior±window."""

from __future__ import annotations

from itertools import product

import numpy as np

from cqa.rubric import DEFAULT_CUTPOINTS
from cqa.scorer import grade_from_score
from eval.metrics import exact_acc, mae

Cutpoints = tuple[float, float, float]


def _predict(scores: list[float], cp: Cutpoints) -> list[int]:
    return [int(grade_from_score(s, cp)) for s in scores]


def tune_cutpoints(
    scores: list[float],
    y: list[int],
    prior: Cutpoints = DEFAULT_CUTPOINTS,
    window: float = 8.0,
    step: float = 1.0,
) -> Cutpoints:
    """Grid search minimising MAE; ties go to the candidate closest (L1) to the prior."""
    grids = [np.arange(p - window, p + window + 1e-9, step) for p in prior]
    best: tuple[float, float, Cutpoints] | None = None
    for cand in product(*grids):
        cp = tuple(round(float(c), 6) for c in cand)
        if not (cp[0] < cp[1] < cp[2]):
            continue
        err = mae(y, _predict(scores, cp))
        dist = sum(abs(a - b) for a, b in zip(cp, prior))
        key = (err, dist)
        if best is None or key < best[:2]:
            best = (err, dist, cp)
    return best[2] if best else prior


def loo_eval(scores: list[float], y: list[int], prior: Cutpoints = DEFAULT_CUTPOINTS, window: float = 8.0) -> dict:
    prior_preds = _predict(scores, prior)
    loo_preds = []
    for i in range(len(scores)):
        rest_s = scores[:i] + scores[i + 1 :]
        rest_y = y[:i] + y[i + 1 :]
        cp = tune_cutpoints(rest_s, rest_y, prior, window)
        loo_preds.append(int(grade_from_score(scores[i], cp)))
    prior_exact = sum(a == b for a, b in zip(y, prior_preds))
    loo_exact = sum(a == b for a, b in zip(y, loo_preds))
    return {
        "prior_preds": prior_preds,
        "loo_preds": loo_preds,
        "prior_exact": prior_exact,
        "loo_exact": loo_exact,
        "prior_mae": mae(y, prior_preds),
        "loo_mae": mae(y, loo_preds),
        "prior_acc": exact_acc(y, prior_preds),
        "loo_acc": exact_acc(y, loo_preds),
        "adopt_tuned": loo_exact >= prior_exact + 1,
        "full_data_cutpoints": tune_cutpoints(scores, y, prior, window),
    }
