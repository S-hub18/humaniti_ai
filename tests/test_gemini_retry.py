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
