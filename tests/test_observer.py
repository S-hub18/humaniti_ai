import shutil
import subprocess

import pytest

from cqa.cache import CachedObserver
from cqa.media import MediaRef, probe
from cqa.observer import CallSpec, CallStats, MockObserver, plan_calls
from cqa.rubric import CALL_ITEMS, ITEM_IDS, Status
from cqa.schema import ObserverOutput

MEDIA = MediaRef(path="x.mp4", sha256="abc", duration=3000.0)


def test_focus_windows_clamped_short_video():
    specs = {s.name: s for s in plan_calls(600.0)}
    assert (specs["opening"].start, specs["opening"].end) == (0.0, 600.0)
    assert (specs["closing"].start, specs["closing"].end) == (0.0, 600.0)
    assert all(s.start < s.end for s in specs.values())


def test_plan_calls_long_video():
    specs = {s.name: s for s in plan_calls(3000.0)}
    assert (specs["main"].start, specs["main"].end, specs["main"].media_resolution) == (0.0, 3000.0, "low")
    assert (specs["opening"].start, specs["opening"].end, specs["opening"].media_resolution) == (0.0, 600.0, "high")
    assert (specs["closing"].start, specs["closing"].end) == (2400.0, 3000.0)


def test_plan_calls_respects_class_bounds():
    specs = {s.name: s for s in plan_calls(3000.0, class_start=120.0, class_end=2800.0)}
    assert (specs["opening"].start, specs["opening"].end) == (120.0, 720.0)
    assert (specs["closing"].start, specs["closing"].end) == (2200.0, 2800.0)


def test_mock_oracle_emits_call_items():
    oracle = {i: Status.MET for i in ITEM_IDS}
    m = MockObserver(oracle=oracle)
    for spec in plan_calls(3000.0):
        out, stats = m.observe(MEDIA, spec, 0)
        assert set(out.items) == set(CALL_ITEMS[spec.name])
        assert out.window == (spec.start, spec.end)
        assert isinstance(stats, CallStats)
    assert m.calls == 3


def test_mock_oracle_deterministic_with_seed():
    oracle = {i: Status.MET for i in ITEM_IDS}
    spec = plan_calls(3000.0)[0]
    a = MockObserver(oracle=oracle, noise=0.5, seed=7).observe(MEDIA, spec, 2)[0]
    b = MockObserver(oracle=oracle, noise=0.5, seed=7).observe(MEDIA, spec, 2)[0]
    assert a == b
    statuses = {
        MockObserver(oracle=oracle, noise=0.5, seed=7).observe(MEDIA, spec, k)[0].items["homework"].status for k in range(20)
    }
    assert len(statuses) > 1  # noise actually perturbs across samples


def test_mock_script_mode():
    spec = plan_calls(3000.0)[0]
    payload = {"call": "main", "window": [0, 3000], "items": {"homework": {"status": "met", "evidence": [{"t_start": "10:00", "observation": "o"}], "rationale": "r"}}, "session_type": "demo_trial"}
    out, _ = MockObserver(script={"main": [payload]}).observe(MEDIA, spec, 0)
    assert out.session_type == "demo_trial" and out.items["homework"].status == Status.MET


def test_cache_hit_skips_inner(tmp_path):
    inner = MockObserver(oracle={i: Status.MET for i in ITEM_IDS})
    cached = CachedObserver(inner, tmp_path, model="m", prompt_version="v1")
    spec = plan_calls(3000.0)[0]
    first, _ = cached.observe(MEDIA, spec, 0)
    second, stats = cached.observe(MEDIA, spec, 0)
    assert inner.calls == 1 and first == second and stats.cached
    cached.observe(MEDIA, spec, 1)
    assert inner.calls == 2
    assert isinstance(first, ObserverOutput)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
def test_probe_reads_duration(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=duration=3", "-shortest", "-y", str(clip)],
        check=True,
    )
    ref = probe(clip)
    assert ref.duration == pytest.approx(3.0, abs=0.2)
    assert len(ref.sha256) == 64
    assert probe(clip).sha256 == ref.sha256
