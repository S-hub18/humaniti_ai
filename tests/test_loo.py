from eval.loo import loo_eval, tune_cutpoints

PRIOR = (50.0, 68.0, 84.0)


def test_tune_respects_window():
    # data would want an Excellent cut at ~70, which is outside prior±8 -> unreachable,
    # so every allowed value is equally bad and the tie goes back to the prior.
    scores = [70.0, 72.0, 60.0, 61.0, 55.0, 56.0, 20.0, 25.0]
    y = [3, 3, 2, 2, 1, 1, 0, 0]
    cp = tune_cutpoints(scores, y, PRIOR)
    assert all(abs(c - p) <= 8.0 for c, p in zip(cp, PRIOR))
    assert cp[0] < cp[1] < cp[2]
    assert cp[2] == 84.0


def test_tune_moves_within_window():
    scores = [77.0, 78.0, 70.0, 71.0, 55.0, 56.0, 20.0, 25.0]
    y = [3, 3, 2, 2, 1, 1, 0, 0]
    cp = tune_cutpoints(scores, y, PRIOR)
    assert cp[2] == 77.0  # closest-to-prior value that gets both Excellent classes right


def test_tune_prefers_prior_on_ties():
    scores = [95.0, 90.0, 75.0, 72.0, 60.0, 55.0, 10.0, 20.0]
    y = [3, 3, 2, 2, 1, 1, 0, 0]
    assert tune_cutpoints(scores, y, PRIOR) == PRIOR


def test_loo_adopt_rule():
    # priors already perfect -> tuned cannot beat by >= 1 -> keep prior
    scores = [95.0, 90.0, 75.0, 72.0, 60.0, 55.0, 10.0, 20.0]
    y = [3, 3, 2, 2, 1, 1, 0, 0]
    r = loo_eval(scores, y, PRIOR)
    assert r["prior_exact"] == 8 and r["adopt_tuned"] is False
    assert len(r["loo_preds"]) == 8 and len(r["prior_preds"]) == 8


def test_loo_adopts_when_clearly_better():
    # Excellent classes consistently score ~78; Good ~70. Priors call them all Good.
    scores = [78.0, 79.0, 77.0, 70.0, 69.0, 71.0, 55.0, 56.0, 20.0, 25.0]
    y = [3, 3, 3, 2, 2, 2, 1, 1, 0, 0]
    r = loo_eval(scores, y, PRIOR)
    assert r["loo_exact"] >= r["prior_exact"] + 1
    assert r["adopt_tuned"] is True
