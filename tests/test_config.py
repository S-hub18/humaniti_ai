from types import SimpleNamespace

import pytest

from cqa.config import DEFAULT_TIER, MODELS, load_dotenv, model_id, prices_for
from cqa.gemini_observer import GeminiObserver


def test_default_is_flash_and_tiers_defined():
    assert DEFAULT_TIER == "flash"
    assert set(MODELS) == {"flash", "flash-lite"}
    assert all("pro" not in m for m in MODELS.values())


def test_model_id_from_tier_env_and_override(monkeypatch):
    monkeypatch.delenv("CQA_GEMINI_MODEL", raising=False)
    assert model_id() == MODELS["flash"]
    assert model_id("flash-lite") == MODELS["flash-lite"]
    monkeypatch.setenv("CQA_GEMINI_MODEL", "gemini-2.5-flash-lite")
    assert model_id() == "gemini-2.5-flash-lite"


@pytest.mark.parametrize("bad", ["gemini-3-pro", "gemini-2.5-pro-preview", "models/gemini-pro-latest"])
def test_pro_models_rejected(bad, monkeypatch):
    with pytest.raises(ValueError):
        model_id(bad)
    monkeypatch.setenv("CQA_GEMINI_MODEL", bad)
    with pytest.raises(ValueError):
        model_id()
    with pytest.raises(ValueError):
        GeminiObserver(SimpleNamespace(), model=bad)


def test_prices_known_for_both_tiers():
    for m in MODELS.values():
        p = prices_for(m)
        assert p["input_per_m"] > 0 and p["output_per_m"] > 0
    assert prices_for("unknown-flash-model") == {"input_per_m": 0.0, "output_per_m": 0.0}


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFOO_TEST_KEY='abc'\nBAR_TEST_KEY=1\n")
    monkeypatch.setenv("BAR_TEST_KEY", "keep")
    monkeypatch.delenv("FOO_TEST_KEY", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["FOO_TEST_KEY"] == "abc" and os.environ["BAR_TEST_KEY"] == "keep"


def test_free_tier_flag_controls_spacing(monkeypatch):
    from cqa.config import call_spacing_s, free_tier

    monkeypatch.delenv("FREE_TIER", raising=False)
    assert free_tier() is True and call_spacing_s() == 65.0
    monkeypatch.setenv("FREE_TIER", "0")
    assert free_tier() is False and call_spacing_s() == 2.0
    monkeypatch.setenv("FREE_TIER", "true")
    assert free_tier() is True
