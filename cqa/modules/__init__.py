"""Pluggable modules. Each returns a ModuleResult: extra item votes, gate signals and flags.

Contract (spec §6): modules can only add votes (B1, B2) or make camera gates stricter (XC).
They never call external APIs unless the module explicitly says so (B1 only).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cqa.aggregate import ModuleVote
from cqa.scorer import GateSignal


class ModuleResult(BaseModel):
    module: str
    votes: list[ModuleVote] = Field(default_factory=list)
    gates: list[GateSignal] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    features: dict = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
