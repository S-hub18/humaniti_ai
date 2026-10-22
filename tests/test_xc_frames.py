from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from cqa.modules import xc_frames
from cqa.modules.xc_frames import (
    LOGO_PATH,
    analyze_frames,
    crosscheck,
    find_logo,
    sample_frames,
    speaker_region,
)

H, W = 720, 1280


def _logo_img() -> np.ndarray:
    return cv2.imread(str(LOGO_PATH))


def _screen_frame(logo: bool, seed: int = 0) -> np.ndarray:
    """Fake lichess-like screen share with a dark top-right tile, optionally with the logo."""
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), 235, np.uint8)
    for _ in range(60):  # text-like clutter
        x, y = int(rng.integers(0, 1000)), int(rng.integers(130, 680))
        cv2.rectangle(img, (x, y), (x + int(rng.integers(20, 200)), y + 8), (90, 90, 90), -1)
    x0, y1 = int(0.82 * W), int(0.17 * H)
    tile = np.full((y1, W - x0, 3), (30, 18, 12), np.uint8)
    if logo:
        lg = _logo_img()
        lw = 100
        lh = int(lg.shape[0] * lw / lg.shape[1])
        small = cv2.resize(lg, (lw, lh), interpolation=cv2.INTER_AREA)
        tile[10 : 10 + lh, 50 : 50 + lw] = small
    img[:y1, x0:] = tile
    return img


def _full_frame(logo: bool) -> np.ndarray:
    img = np.full((H, W, 3), (30, 18, 12), np.uint8)
    if logo:
        lg = _logo_img()
        lg = cv2.resize(lg, (900, int(lg.shape[0] * 900 / lg.shape[1])))
        img[80 : 80 + lg.shape[0], 190:1090] = lg
    return img


def _no_face(_img):
    return 0


def _one_face(_img):
    return 1


def test_logo_asset_exists():
    assert LOGO_PATH.exists()


def test_find_logo_in_tile_vs_plain_tile():
    f_logo, f_plain = _screen_frame(True), _screen_frame(False)
    assert find_logo(speaker_region(f_logo)[0])
    assert not find_logo(speaker_region(f_plain)[0])


def test_find_logo_full_frame():
    assert find_logo(_full_frame(True))
    assert not find_logo(_full_frame(False))


def test_speaker_region_screenshare_is_tile():
    reg, kind = speaker_region(_screen_frame(False))
    assert kind == "screenshare"
    assert reg.shape[1] < W * 0.25 and reg.shape[0] < H * 0.2


def test_speaker_region_full_frame():
    reg, kind = speaker_region(_full_frame(True))
    assert kind == "full"
    assert reg.shape[:2] == (H, W)


def _frames(n, logo_every=None, logo=False):
    out = []
    for i in range(n):
        has = logo if logo_every is None else (i % logo_every == 0)
        out.append((float(i), _screen_frame(has, seed=i % 3)))
    return out


def _gate(res, field):
    return next(g for g in res.gates if g.field == field)


def test_branded_present_when_logo_in_20pct():
    res = analyze_frames(_frames(40, logo_every=4), face_detector=_one_face)
    g = _gate(res, "branded_background")
    assert g.value == "present" and g.module == "xc" and g.n_frames == 40
    assert res.features["logo_hit_rate"] == pytest.approx(0.25)
    assert res.features["first_logo_t"] == 0.0
    assert res.features["last_logo_t"] == 36.0


def test_branded_absent_requires_logo_zero_30_frames_and_10_faced():
    res = analyze_frames(_frames(40, logo=False), face_detector=_one_face, allow_absent=True)
    assert _gate(res, "branded_background").value == "absent"


def test_absent_is_opt_in_by_default():
    res = analyze_frames(_frames(40, logo=False), face_detector=_one_face)
    assert _gate(res, "branded_background").value == "unclear"


def test_branded_unclear_when_few_frames():
    res = analyze_frames(_frames(20), face_detector=_one_face)
    assert _gate(res, "branded_background").value == "unclear"


def test_branded_unclear_when_no_faced_nonlogo_frames():
    res = analyze_frames(_frames(40), face_detector=_no_face)
    assert _gate(res, "branded_background").value == "unclear"


def test_branded_unclear_between_zero_and_20pct():
    res = analyze_frames(_frames(40, logo_every=20), face_detector=_one_face)  # 2/40 = 5%
    assert _gate(res, "branded_background").value == "unclear"


def test_face_rarely_mostly_unclear():
    assert _gate(analyze_frames(_frames(40), face_detector=_no_face, allow_rarely=True), "face_visible").value == "rarely"
    assert _gate(analyze_frames(_frames(40), face_detector=_no_face), "face_visible").value == "unclear"  # opt-in
    assert _gate(analyze_frames(_frames(40), face_detector=_one_face), "face_visible").value == "mostly"
    assert _gate(analyze_frames(_frames(20), face_detector=_no_face), "face_visible").value == "unclear"
    flip = iter([1, 0] * 20)
    res = analyze_frames(_frames(40), face_detector=lambda _i: next(flip))
    assert _gate(res, "face_visible").value == "unclear"


def test_empty_frames_are_unclear():
    res = analyze_frames([], face_detector=_one_face)
    assert all(g.value == "unclear" and g.n_frames == 0 for g in res.gates)


def test_features_counts_frame_types():
    frames = _frames(5) + [(9.0, _full_frame(True))]
    res = analyze_frames(frames, face_detector=_no_face)
    assert res.features["frame_types"] == {"screenshare": 5, "full": 1}


def test_real_haar_detector_runs_on_blank():
    res = analyze_frames(_frames(3))
    assert res.ok and res.features["face_hit_rate"] == 0.0


def _make_clip(path: Path, secs: int = 10):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=10:duration={secs}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def test_sample_frames_returns_n_increasing(tmp_path):
    clip = tmp_path / "c.mp4"
    _make_clip(clip)
    out = sample_frames(clip, n=8, out_dir=tmp_path / "fr")
    assert len(out) == 8
    ts = [t for t, _ in out]
    assert ts == sorted(ts) and len(set(ts)) == 8
    assert all(p.exists() for _, p in out)
    assert cv2.imread(str(out[0][1])).shape[1] == 1280


def test_crosscheck_deletes_temp_frames(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp4"
    _make_clip(clip, 4)
    seen = {}
    real = xc_frames.sample_frames

    def spy(media, n, out_dir):
        seen["dir"] = Path(out_dir)
        return real(media, n, out_dir)

    monkeypatch.setattr(xc_frames, "sample_frames", spy)
    res = crosscheck(clip, n=4)
    assert res.module == "xc"
    assert not seen["dir"].exists()


def test_crosscheck_deletes_temp_frames_on_exception(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp4"
    _make_clip(clip, 4)
    seen = {}
    real = xc_frames.sample_frames

    def spy(media, n, out_dir):
        seen["dir"] = Path(out_dir)
        return real(media, n, out_dir)

    def boom(frames, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(xc_frames, "sample_frames", spy)
    monkeypatch.setattr(xc_frames, "analyze_frames", boom)
    with pytest.raises(RuntimeError):
        crosscheck(clip, n=4)
    assert not seen["dir"].exists()
