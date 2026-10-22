"""Model tiers and pricing. Hard constraint: Flash / Flash-Lite only, never Pro.

Prices: Gemini API paid tier, per 1M tokens, from ai.google.dev/gemini-api/docs/pricing (checked 2026-10-22).
gemini-3.8-flash prices rise on 2027-01-01 (input $1.50, output $7.50).
"""

from __future__ import annotations

import os
from pathlib import Path

MODELS: dict[str, str] = {
    "flash": "gemini-3.8-flash",
    "flash-lite": "gemini-3.5-flash-lite",
}
DEFAULT_TIER = "flash"
MODEL_ENV = "CQA_GEMINI_MODEL"

PRICES: dict[str, dict[str, float]] = {
    "gemini-3.8-flash": {"input_per_m": 0.75, "output_per_m": 3.75},
    "gemini-3.5-flash-lite": {"input_per_m": 0.30, "output_per_m": 2.50},
    "gemini-2.5-flash": {"input_per_m": 0.30, "output_per_m": 2.50},  # audio input is $1.00/M
    "gemini-2.5-flash-lite": {"input_per_m": 0.10, "output_per_m": 0.40},  # audio input is $0.30/M
}


def _check(model: str) -> str:
    name = model.split("/")[-1].lower()
    if "pro" in name.split("-") or "-pro" in name or "pro-" in name:
        raise ValueError(f"Pro models are not allowed (got {model!r}); use a Flash or Flash-Lite model")
    if "flash" not in name:
        raise ValueError(f"only Flash / Flash-Lite models are allowed (got {model!r})")
    return model


def model_id(tier_or_model: str | None = None) -> str:
    """Resolve a tier ('flash'|'flash-lite') or explicit model id; env CQA_GEMINI_MODEL overrides the default."""
    if tier_or_model is None:
        return _check(os.environ.get(MODEL_ENV) or MODELS[DEFAULT_TIER])
    return _check(MODELS.get(tier_or_model, tier_or_model))


def prices_for(model: str) -> dict[str, float]:
    return dict(PRICES.get(model.split("/")[-1], {"input_per_m": 0.0, "output_per_m": 0.0}))


def load_dotenv(path: Path | str = Path(__file__).resolve().parents[1] / ".env") -> None:
    """Minimal .env loader (no dependency). Never prints values; never overrides existing env vars."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))
