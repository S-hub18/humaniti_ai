import pytest

from cqa.gemini_retry import countdown_sleep, retry_call


class RL(Exception):
    code = 429


def test_retry_reports_live_countdown():
    ticks = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RL("429 RESOURCE_EXHAUSTED 'retryDelay': '3s'")
        return "ok"

    out = retry_call(fn, "test", sleep=lambda s: None, on_wait=lambda rem, a, m, kind: ticks.append((rem, a, m, kind)))
    assert out == "ok"
    rems = [t[0] for t in ticks]
    assert rems[0] == 5 and rems == sorted(rems, reverse=True) and rems[-1] == 1
    assert all(t[1:] == (1, 4, "rate_limit") for t in ticks)


def test_countdown_sleep_ticks_each_second():
    seen, slept = [], []
    countdown_sleep(3.4, lambda rem: seen.append(rem), sleep=slept.append)
    assert seen == [4, 3, 2, 1] and pytest.approx(sum(slept)) == 3.4


# ---- proactive pacing (token bucket), cap, full 429 logging, cancellation ----
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_bucket_allows_under_budget_and_waits_when_over():
    from cqa.gemini_retry import TokenBucket

    clk = FakeClock()
    b = TokenBucket(tpm=200_000, clock=clk, sleep=clk.sleep)
    b.acquire(150_000)
    assert clk.t == 0.0
    ticks = []
    b.acquire(100_000, on_wait=ticks.append)  # 250k > 200k: must wait for the first call to leave the window
    assert clk.t == pytest.approx(60.0) and ticks[0] == 60 and ticks[-1] == 1


def test_bucket_lets_one_oversized_call_through_when_idle():
    from cqa.gemini_retry import TokenBucket

    clk = FakeClock()
    b = TokenBucket(tpm=200_000, clock=clk, sleep=clk.sleep)
    b.acquire(340_000)  # a full 56-min video main call alone exceeds the budget; allowed when nothing else is in flight
    assert clk.t == 0.0
    b.acquire(10_000)
    assert clk.t == pytest.approx(60.0)


def test_tpm_budget_env(monkeypatch):
    from cqa.gemini_retry import tpm_budget

    monkeypatch.setenv("GEMINI_TPM", "123")
    assert tpm_budget() == 123
    monkeypatch.delenv("GEMINI_TPM")
    monkeypatch.delenv("FREE_TIER", raising=False)
    assert tpm_budget() == 200_000
    monkeypatch.setenv("FREE_TIER", "0")
    assert tpm_budget() > 1_000_000


def test_backoff_capped_at_90s():
    from cqa.gemini_retry import retry_delay

    class RL(Exception):
        code = 429

    for attempt in range(6):
        assert retry_delay(RL("429 RESOURCE_EXHAUSTED 'retryDelay': '161s'"), attempt) <= 90.0


def test_429_full_error_logged(caplog):
    import logging

    msg = ("429 RESOURCE_EXHAUSTED Quota exceeded for metric: generativelanguage.googleapis.com/"
           "generate_content_free_tier_input_token_count, limit: 250000 'retryDelay': '54s'")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RL(msg)
        return "ok"

    with caplog.at_level(logging.WARNING, logger="cqa.gemini"):
        retry_call(fn, "test", sleep=lambda s: None)
    assert any(msg in r.getMessage() for r in caplog.records)
