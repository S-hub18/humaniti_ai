"""Metamorphic / counterfactual / invariance variant generator (spec §7.1).

usage:
  uv run python -m eval.variants --list
  uv run python -m eval.variants --make B472_cut_homework [--dry-run]

Variant files contain children and are written ONLY under .scratch/eval_variants/.
Do not run --make on everything casually: each re-encode of a 60-min class takes minutes.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import yaml

from cqa.rubric import ORDINAL, Grade
from cqa.scorer import Assessment

REPO = Path(__file__).resolve().parents[1]
SAFE_ROOT = REPO / ".scratch" / "eval_variants"
# Speaker tile region as fractions of the frame (measured on 1280x720 frames: x>=1065, y<=120).
TILE = {"x": 0.82, "y": 0.0, "w": 0.18, "h": 0.18}
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "96k"]
OPS = ("trim_opening", "cut_segment", "blackout_tile", "cut_tail", "pad_tail", "reencode_low")


def build_cmd(src: Path, op: str, params: dict, out: Path) -> list[str]:
    base = ["ffmpeg", "-v", "error", "-y"]
    if op == "trim_opening":
        return base + ["-ss", str(params["start_s"]), "-i", str(src), "-c", "copy", str(out)]
    if op == "cut_segment":
        a, b = params["start_s"], params["end_s"]
        keep = f"not(between(t,{a},{b}))"
        return base + ["-i", str(src),
                       "-vf", f"select='{keep}',setpts=N/FRAME_RATE/TB",
                       "-af", f"aselect='{keep}',asetpts=N/SR/TB", *ENC, str(out)]
    if op == "blackout_tile":
        box = f"drawbox=x=iw*{TILE['x']}:y=ih*{TILE['y']}:w=iw*{TILE['w']}:h=ih*{TILE['h']}:color=black:t=fill"
        return base + ["-i", str(src), "-vf", box, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "copy", str(out)]
    if op == "cut_tail":
        return base + ["-i", str(src), "-t", str(params["end_s"]), "-c", "copy", str(out)]
    if op == "pad_tail":
        pad = params["pad_s"]
        return base + ["-i", str(src), "-vf", f"tpad=stop_mode=add:stop_duration={pad}:color=black",
                       "-af", f"apad=pad_dur={pad}", *ENC, str(out)]
    if op == "reencode_low":
        return base + ["-i", str(src), "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "300k",
                       "-c:a", "aac", "-ac", "1", "-b:a", "32k", str(out)]
    raise ValueError(f"unknown op {op!r}")


def make_variant(src: Path, op: str, params: dict, out_dir: Path, dry_run: bool = False,
                 safe_root: Path = SAFE_ROOT, name: str | None = None) -> Path:
    out_dir = Path(out_dir).resolve()
    root = Path(safe_root).resolve()
    if out_dir != root and root not in out_dir.parents:
        raise ValueError(f"variants may only be written under {root}")
    out = out_dir / f"{name or Path(src).stem + '__' + op}.mp4"
    cmd = build_cmd(Path(src), op, params, out)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True)
    return out


def _ord(status) -> int | None:
    return ORDINAL.get(status)


def check_expectation(base: Assessment, var: Assessment, expect: dict) -> bool:
    d = expect["direction"]
    if d == "down":
        if "item" in expect:
            b, v = _ord(base.items[expect["item"]].status), _ord(var.items[expect["item"]].status)
            return b is not None and v is not None and v < b
        return (var.score or 0) < (base.score or 0)
    if d == "flip_to_not_met":
        b, v = _ord(base.items[expect["item"]].status), _ord(var.items[expect["item"]].status)
        return b == 2 and v is not None and v < 2
    if d == "gate_fires":
        return var.grade == Grade.POOR and any(f.startswith("compliance:") for f in var.flags)
    if d == "unchanged":
        tol = expect.get("tol", 5)
        return var.grade == base.grade and abs((var.score or 0) - (base.score or 0)) <= tol
    raise ValueError(f"unknown direction {d!r}")


def load_variants(path: Path = REPO / "eval" / "variants.yaml") -> list[dict]:
    return yaml.safe_load(Path(path).read_text())["variants"]


def main(argv: list[str] | None = None) -> None:
    from eval.run_eval import load_dataset, resolve_media

    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--make", nargs="*", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    variants = {v["id"]: v for v in load_variants()}
    if args.list:
        for v in variants.values():
            print(f"{v['id']}: {v['op']} {v['params']} expect {v['expect']}")
    entries = {e["id"]: e for e in load_dataset(REPO / "eval" / "dataset.yaml")}
    for vid in args.make:
        v = variants[vid]
        src = resolve_media(entries[v["source_class"]]).path
        out = make_variant(src, v["op"], v["params"], SAFE_ROOT, dry_run=args.dry_run, name=vid)
        print(("DRY " if args.dry_run else "") + " ".join(build_cmd(src, v["op"], v["params"], out)) if args.dry_run else out)


if __name__ == "__main__":
    main()
