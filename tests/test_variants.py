import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from cqa.aggregate import ItemConsensus
from cqa.rubric import ITEM_IDS, Grade, Status
from cqa.schema import CameraObs
from cqa.scorer import Assessment
from eval.variants import OPS, SAFE_ROOT, build_cmd, check_expectation, make_variant

REPO = Path(__file__).resolve().parents[1]


def A(grade=Grade.EXCELLENT, score=90.0, flags=(), **statuses):
    items = {i: ItemConsensus(status=statuses.get(i, Status.MET), agreement=1.0) for i in ITEM_IDS}
    return Assessment(grade=grade, score=score, session_type="regular_class", confidence="high", flags=list(flags),
                      abstain_reason=None, items=items, camera=CameraObs(), coverage=1.0)


def test_variant_path_guard(tmp_path):
    with pytest.raises(ValueError):
        make_variant(Path("x.mp4"), "cut_tail", {"end_s": 10}, tmp_path / "out", dry_run=True)
    with pytest.raises(ValueError):
        make_variant(Path("x.mp4"), "cut_tail", {"end_s": 10}, SAFE_ROOT / ".." / "escape", dry_run=True)
    p = make_variant(Path("x.mp4"), "cut_tail", {"end_s": 10}, SAFE_ROOT / "t", dry_run=True)
    assert SAFE_ROOT.resolve() in p.resolve().parents


def test_ffmpeg_cmd_for_each_op():
    params = {
        "trim_opening": {"start_s": 360}, "cut_segment": {"start_s": 2700, "end_s": 2860},
        "blackout_tile": {}, "cut_tail": {"end_s": 4100}, "pad_tail": {"pad_s": 300}, "reencode_low": {},
    }
    assert set(params) == set(OPS)
    for op, prm in params.items():
        cmd = build_cmd(Path("in.mp4"), op, prm, Path("out.mp4"))
        assert cmd[0] == "ffmpeg" and cmd[-1] == "out.mp4" and "in.mp4" in cmd
    assert "drawbox" in " ".join(build_cmd(Path("i.mp4"), "blackout_tile", {}, Path("o.mp4")))
    assert "between(t,2700,2860)" in " ".join(build_cmd(Path("i.mp4"), "cut_segment", params["cut_segment"], Path("o.mp4")))
    with pytest.raises(ValueError):
        build_cmd(Path("i.mp4"), "explode", {}, Path("o.mp4"))


def test_check_expectation_directions():
    base = A()
    assert check_expectation(base, A(rapport_intro=Status.PARTIAL, score=80), {"item": "rapport_intro", "direction": "down"})
    assert not check_expectation(base, A(), {"item": "rapport_intro", "direction": "down"})
    assert check_expectation(base, A(homework=Status.NOT_MET), {"item": "homework", "direction": "flip_to_not_met"})
    assert not check_expectation(base, A(homework=Status.NOT_OBSERVABLE), {"item": "homework", "direction": "flip_to_not_met"})
    assert check_expectation(base, A(grade=Grade.POOR, flags=["compliance:face_not_visible"]), {"direction": "gate_fires"})
    assert not check_expectation(base, A(grade=Grade.POOR), {"direction": "gate_fires"})
    assert check_expectation(base, A(score=87.0), {"direction": "unchanged", "tol": 5})
    assert not check_expectation(base, A(score=80.0, grade=Grade.GOOD), {"direction": "unchanged", "tol": 5})


def test_variants_yaml_valid():
    spec = yaml.safe_load((REPO / "eval" / "variants.yaml").read_text())
    ids = [v["id"] for v in spec["variants"]]
    assert len(ids) == len(set(ids)) >= 8
    for v in spec["variants"]:
        assert v["op"] in OPS and v["expect"]["direction"] in ("down", "flip_to_not_met", "gate_fires", "unchanged")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
def test_make_variant_on_synthetic_clip(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=4", "-shortest", "-y", str(src)], check=True)
    out = make_variant(src, "blackout_tile", {}, tmp_path / "variants", safe_root=tmp_path)
    assert out.exists() and out.stat().st_size > 0
    out2 = make_variant(src, "cut_segment", {"start_s": 1, "end_s": 2}, tmp_path / "variants", safe_root=tmp_path)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out2)],
                               capture_output=True, text=True).stdout)
    assert dur == pytest.approx(3.0, abs=0.4)
