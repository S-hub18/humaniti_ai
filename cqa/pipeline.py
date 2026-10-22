"""Core pipeline: plan calls -> observe k times -> validate -> merge -> vote -> score."""

from __future__ import annotations

from contextlib import nullcontext

from pydantic import BaseModel, Field

from cqa.aggregate import MergedRun, ModuleVote, merge_calls, vote
from cqa.evidence import validate
from cqa.media import MediaRef
from cqa.observer import CallStats, Observer, plan_calls
from cqa.schema import TranscriptTurn
from cqa.scorer import Assessment, GateSignal, ScoringConfig, score


class PipelineConfig(BaseModel):
    k: int = 3
    sample_offset: int = 0
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    modules: dict[str, bool] = Field(default_factory=lambda: {"b1": False, "b2": False, "xc": False})
    class_start: float | None = None
    class_end: float | None = None


class AssessmentRun(BaseModel):
    assessment: Assessment
    runs: list[MergedRun]
    stats: list[CallStats]
    flags: list[str]


def observe_runs(
    media: MediaRef,
    observer: Observer,
    cfg: PipelineConfig,
    transcript: list[TranscriptTurn] | None = None,
) -> tuple[list[MergedRun], list[CallStats], list[str]]:
    session = getattr(observer, "session", None)
    ctx = session(media) if session is not None else nullcontext()
    with ctx:
        return _observe_runs(media, observer, cfg, transcript)


def _observe_runs(media, observer, cfg, transcript):
    specs = plan_calls(media.duration, class_start=cfg.class_start, class_end=cfg.class_end)
    runs, stats, flags = [], [], []
    for idx in range(cfg.sample_offset, cfg.sample_offset + cfg.k):
        outs = {}
        for spec in specs:
            out, st = observer.observe(media, spec, idx)
            out, fl = validate(out, media.duration, transcript)
            outs[spec.name] = out
            stats.append(st)
            flags += [f for f in fl if f not in flags]
        runs.append(merge_calls(outs["main"], outs.get("opening"), outs.get("closing")))
    return runs, stats, flags


def assess(
    media: MediaRef,
    observer: Observer,
    cfg: PipelineConfig | None = None,
    transcript: list[TranscriptTurn] | None = None,
    module_votes: list[ModuleVote] | None = None,
    gates: list[GateSignal] | None = None,
    extra_flags: list[str] | None = None,
) -> AssessmentRun:
    cfg = cfg or PipelineConfig()
    runs, stats, flags = observe_runs(media, observer, cfg, transcript)
    consensus = vote(runs, module_votes or [])
    flags = flags + [f for f in (extra_flags or []) if f not in flags]
    assessment = score(consensus, cfg.scoring, gates or [], flags)
    return AssessmentRun(assessment=assessment, runs=runs, stats=stats, flags=flags)
