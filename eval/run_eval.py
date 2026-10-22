"""Eval runner: assess every labelled class, compute metrics, LOO, variance, item accuracy, cost, ablation.

usage:
  uv run python -m eval.run_eval --observer mock
  uv run python -m eval.run_eval --observer gemini --samples 6 --k 3 [--include-holdout]

Outputs go to .scratch/eval_runs/<timestamp>/ (gitignored: results describe real classes).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, Field

from cqa.aggregate import MergedRun, ModuleVote, vote
from cqa.media import MediaRef, probe
from cqa.observer import MockObserver, Observer
from cqa.pipeline import PipelineConfig, assess
from cqa.rubric import ITEM_IDS, Status
from cqa.scorer import GateSignal, ScoringConfig, grade_from_score, score
from eval.loo import loo_eval
from eval.metrics import exact_acc, item_accuracy, mae, majority_baseline, pairwise_acc, spearman, within1_acc

REPO = Path(__file__).resolve().parents[1]
BANNER = "Grade-level accuracy rests on 10 real labels; item-level, pairwise and variant results are supporting evidence only."


class EvalConfig(BaseModel):
    samples: int = 6
    k: int = 3
    include_holdout: bool = False
    observer_kind: str = "mock"
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)


def load_dataset(path: Path | str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text())
    root = REPO / data["root"]
    return [{**c, "root": str(root)} for c in data["classes"]]


def resolve_media(entry: dict) -> MediaRef:
    folder = Path(entry["root"]) / entry["folder"]
    matches = sorted(folder.glob(entry["video_glob"]))
    if len(matches) != 1:
        raise FileNotFoundError(f"{entry['id']}: expected 1 video for {entry['video_glob']!r}, found {len(matches)}")
    return probe(matches[0])


def _read_rows(path: Path, ids: set[str]) -> list[dict]:
    if not path.exists():
        return []
    return [r for r in csv.DictReader(path.open()) if r["class"] in ids and r["status"] in ("met", "partial", "not_met")]


def _pred_grade(a, cutpoints: tuple[float, float, float]) -> int:
    """Abstained classes are scored by their raw score so they still count (and usually count as misses)."""
    if a.grade is not None:
        return int(a.grade)
    return int(grade_from_score(a.score or 0.0, cutpoints))


def _grade_metrics(y: list[int], p: list[int], s: list[float]) -> dict:
    pw, pw_n = pairwise_acc(y, s)
    return {
        "n": len(y),
        "exact": exact_acc(y, p),
        "within1": within1_acc(y, p),
        "mae": mae(y, p),
        "spearman": spearman(y, s),
        "pairwise": pw,
        "pairwise_n": pw_n,
    }


def ablate(
    runs_by_class: dict[str, list[MergedRun]],
    labels: dict[str, int | None],
    votes_by_class: dict[str, list[ModuleVote]],
    gates_by_class: dict[str, list[GateSignal]],
    configs: dict[str, set[str]],
    scoring: ScoringConfig | None = None,
) -> list[dict]:
    """Re-score cached runs with modules toggled. No observer calls."""
    scoring = scoring or ScoringConfig()
    rows = []
    for name, mods in configs.items():
        y, p, s = [], [], []
        for cid, runs in runs_by_class.items():
            if labels.get(cid) is None:
                continue
            c = vote(runs, [v for v in votes_by_class.get(cid, []) if v.module in mods])
            a = score(c, scoring, [g for g in gates_by_class.get(cid, []) if g.module in mods])
            y.append(labels[cid])
            p.append(_pred_grade(a, scoring.cutpoints))
            s.append(a.score or 0.0)
        rows.append({"config": name, **_grade_metrics(y, p, s)})
    return rows


def run_eval(
    entries: list[dict],
    observer_factory: Callable[[dict], Observer],
    cfg: EvalConfig,
    media_resolver: Callable[[dict], MediaRef] = resolve_media,
    gt_path: Path = REPO / "eval" / "ground_truth_items.csv",
    silver_path: Path = REPO / "eval" / "silver_items.csv",
    b1_provider: Callable[[dict, MediaRef], "ModuleResult"] | None = None,
) -> dict:
    entries = [e for e in entries if cfg.include_holdout or not e["holdout"]]
    classes, runs_by_class, labels, b1_votes = [], {}, {}, {}
    flips, diffs = [], []
    for e in entries:
        obs = observer_factory(e)
        media = media_resolver(e)
        t0 = time.monotonic()
        run_a = assess(media, obs, PipelineConfig(k=cfg.k, sample_offset=0, scoring=cfg.scoring))
        wall = time.monotonic() - t0
        a = run_a.assessment
        rec = {
            "id": e["id"], "label": e["label"], "ordinal": e["ordinal"], "holdout": e["holdout"],
            "grade": int(a.grade) if a.grade is not None else None, "score": a.score,
            "session_type": a.session_type, "confidence": a.confidence, "flags": a.flags,
            "abstain_reason": a.abstain_reason,
            "items": {i: a.items[i].status.value for i in ITEM_IDS},
            "agreement": {i: round(a.items[i].agreement, 3) for i in ITEM_IDS},
            "cost_usd": sum(st.cost_usd for st in run_a.stats),
            "latency_s": sum(st.latency_s for st in run_a.stats) or wall,
        }
        if cfg.samples >= 2 * cfg.k:
            b = assess(media, obs, PipelineConfig(k=cfg.k, sample_offset=cfg.k, scoring=cfg.scoring)).assessment
            rec["grade_b"] = int(b.grade) if b.grade is not None else None
            rec["score_b"] = b.score
            if e["ordinal"] is not None:
                flips.append(rec["grade"] != rec["grade_b"])
                diffs.append(abs((a.score or 0) - (b.score or 0)))
        if b1_provider is not None:
            b1 = b1_provider(e, media)
            b1_votes[e["id"]] = b1.votes if b1.ok else []
            rec["b1_ok"] = b1.ok
        classes.append(rec)
        runs_by_class[e["id"]] = run_a.runs
        labels[e["id"]] = e["ordinal"]

    graded = [c for c in classes if c["ordinal"] is not None]
    y = [c["ordinal"] for c in graded]
    s = [c["score"] or 0.0 for c in graded]
    p = [c["grade"] if c["grade"] is not None else int(grade_from_score(c["score"] or 0.0, cfg.scoring.cutpoints)) for c in graded]
    metrics = _grade_metrics(y, p, s)
    base = majority_baseline(y)
    metrics.update(
        baseline_exact=exact_acc(y, base), baseline_within1=within1_acc(y, base), baseline_mae=mae(y, base),
        n_abstain=sum(c["grade"] is None for c in graded),
    )

    ids = {c["id"] for c in graded}
    pred = {c["id"]: {i: Status(v) for i, v in c["items"].items()} for c in graded}
    items = {}
    for src, path in (("grader", gt_path), ("silver", silver_path)):
        rows = _read_rows(path, ids)
        if rows:
            items[src] = {"strict": item_accuracy(rows, pred, lenient=False), "lenient": item_accuracy(rows, pred, lenient=True)}

    lat = [c["latency_s"] for c in classes]
    cost = [c["cost_usd"] for c in classes]
    return {
        "observer": cfg.observer_kind,
        "config": cfg.model_dump(),
        "classes": classes,
        "metrics": metrics,
        "loo": loo_eval(s, y, cfg.scoring.cutpoints) if len(y) >= 3 else {},
        "variance": {
            "n": len(flips),
            "flip_rate": (sum(flips) / len(flips)) if flips else None,
            "mean_abs_score_diff": statistics.mean(diffs) if diffs else None,
        },
        "items": items,
        "cost_latency": {
            "cost_per_class_usd": statistics.mean(cost) if cost else 0.0,
            "latency_p50_s": statistics.median(lat) if lat else 0.0,
            "latency_max_s": max(lat) if lat else 0.0,
        },
        "ablation": ablate(runs_by_class, labels, b1_votes, {},
                           {"core": set(), "core+b1": {"b1"}} if b1_provider else {"core": set()}, cfg.scoring),
        "_runs": runs_by_class,
        "_labels": labels,
    }


def _fmt(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and x != x) else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def write_report(result: dict, out_dir: Path | str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    (out / "results.json").write_text(json.dumps(public, indent=1, default=str))
    m, L = result["metrics"], []
    L.append("# Eval report")
    if result["observer"] == "mock":
        L.append("\n> **MOCK RUN**: synthetic observer output. These numbers are NOT results.")
    L.append(f"\n> {BANNER}")
    L.append("\n## Grades\n\n| class | label | y | pred | score | conf | flags |\n|---|---|---|---|---|---|---|")
    for c in result["classes"]:
        L.append(f"| {c['id']}{' (holdout)' if c['holdout'] else ''} | {c['label']} | {_fmt(c['ordinal'])} | {_fmt(c['grade'])} | "
                 f"{_fmt(c['score'], 1)} | {c['confidence']} | {', '.join(c['flags']) or '-'} |")
    L.append("\n## Metrics\n")
    L.append(f"n={m['n']} (abstained {m['n_abstain']}) | exact {_fmt(m['exact'])} (baseline {_fmt(m['baseline_exact'])}) | "
             f"within-1 {_fmt(m['within1'])} (baseline {_fmt(m['baseline_within1'])}) | MAE {_fmt(m['mae'])} "
             f"(baseline {_fmt(m['baseline_mae'])}) | Spearman {_fmt(m['spearman'])} | pairwise {_fmt(m['pairwise'])} over {m['pairwise_n']} pairs")
    loo = result["loo"]
    L.append("\n## LOO cutpoints\n")
    if loo:
        L.append(f"prior exact {loo['prior_exact']} vs LOO exact {loo['loo_exact']} -> adopt tuned: {loo['adopt_tuned']} "
                 f"(full-data tuned cutpoints {loo['full_data_cutpoints']})")
    v = result["variance"]
    L.append(f"\n## Run variance\n\nassessments A (samples 0..k-1) vs B (k..2k-1) on {v['n']} classes: grade flip rate "
             f"{_fmt(v['flip_rate'])}, mean |Δscore| {_fmt(v['mean_abs_score_diff'], 1)}")
    L.append("\n## Item accuracy\n")
    for src, d in result["items"].items():
        L.append(f"\n**{src}** labels (strict / lenient, n):\n")
        for item in ITEM_IDS:
            if item in d["strict"]:
                (sa, n), (la, _) = d["strict"][item], d["lenient"][item]
                L.append(f"- {item}: {_fmt(sa)} / {_fmt(la)} (n={n})")
    cl = result["cost_latency"]
    L.append(f"\n## Cost and latency\n\n${_fmt(cl['cost_per_class_usd'], 3)} per class (assessment A); latency p50 "
             f"{_fmt(cl['latency_p50_s'], 1)} s, max {_fmt(cl['latency_max_s'], 1)} s")
    L.append("\n## Ablation\n\n| config | n | exact | within-1 | MAE | Spearman | pairwise |\n|---|---|---|---|---|---|---|")
    for r in result["ablation"]:
        L.append(f"| {r['config']} | {r['n']} | {_fmt(r['exact'])} | {_fmt(r['within1'])} | {_fmt(r['mae'])} | {_fmt(r['spearman'])} | {_fmt(r['pairwise'])} |")
    L.append("\n## Out-of-distribution (demos)\n")
    for c in result["classes"]:
        if c["ordinal"] is None:
            L.append(f"- {c['id']}: session_type={c['session_type']}, grade={_fmt(c['grade'])}, abstain={c['abstain_reason']}")
    path = out / "report.md"
    path.write_text("\n".join(L) + "\n")
    return path


def _gt_oracle_factory() -> Callable[[dict], Observer]:
    rows = list(csv.DictReader((REPO / "eval" / "ground_truth_items.csv").open()))

    def factory(e: dict) -> Observer:
        oracle = {i: Status.PARTIAL for i in ITEM_IDS}
        oracle.update({r["item"]: Status(r["status"]) for r in rows if r["class"] == e["id"]})
        return MockObserver(oracle=oracle, noise=0.15, seed=sum(map(ord, e["id"])),
                            session_type="demo_trial" if e["label"] == "demo" else "regular_class")

    return factory


def _gemini_factory() -> Callable[[dict], Observer]:
    from google import genai

    from cqa.cache import CachedObserver
    from cqa.gemini_observer import GeminiObserver
    from cqa.prompts import PROMPT_VERSION

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": 600_000})  # ms; no hung calls
    prices = yaml.safe_load((REPO / "cqa" / "pricing.yaml").read_text()) if (REPO / "cqa" / "pricing.yaml").exists() else None
    observer = CachedObserver(GeminiObserver(client, prices=prices), REPO / ".scratch" / "cache",
                              model=os.environ["CQA_GEMINI_MODEL"], prompt_version=PROMPT_VERSION)
    return lambda e: observer


def _b1_provider(client, model: str) -> Callable:
    """B1 per class with a disk cache (eval only; the app never caches)."""
    from cqa.modules import ModuleResult
    from cqa.modules.b1_transcript import B1_PROMPT_VERSION, run_b1

    root = REPO / ".scratch" / "cache" / "b1"
    root.mkdir(parents=True, exist_ok=True)

    def provide(e: dict, media: MediaRef) -> ModuleResult:
        path = root / f"{media.sha256}-{model}-{B1_PROMPT_VERSION}.json"
        if path.exists():
            return ModuleResult.model_validate_json(path.read_text())
        res = run_b1(media.path, None, client, model, media.duration, min_interval_s=65.0)
        path.write_text(res.model_dump_json())
        return res

    return provide


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--observer", choices=["mock", "gemini"], default="mock")
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--include-holdout", action="store_true")
    ap.add_argument("--b1", action="store_true", help="also run the B1 transcript judge (Gemini; audio transcription, no .vtt)")
    ap.add_argument("--out", default=str(REPO / ".scratch" / "eval_runs" / time.strftime("%Y%m%d-%H%M%S")))
    args = ap.parse_args(argv)
    cfg = EvalConfig(samples=args.samples, k=args.k, include_holdout=args.include_holdout, observer_kind=args.observer)
    entries = load_dataset(REPO / "eval" / "dataset.yaml")
    if args.observer == "mock":
        factory = _gt_oracle_factory()
        resolver = lambda e: MediaRef(path=f"{e['id']}.mp4", sha256=e["id"], duration=3000.0)  # noqa: E731
    else:
        factory, resolver = _gemini_factory(), resolve_media
    b1 = None
    if args.b1 and args.observer == "gemini":
        from google import genai

        from cqa.config import model_id

        b1 = _b1_provider(genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": 600_000}), model_id())
    result = run_eval(entries, factory, cfg, media_resolver=resolver, b1_provider=b1)
    print(write_report(result, args.out))


if __name__ == "__main__":
    main()
