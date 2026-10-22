"""XC local crosscheck: face + 8x8 branded-background checks on sampled frames (spec §6).

Runs fully locally (OpenCV Haar face detector + multi-scale logo template match).
It can only make the camera gates STRICTER (the scorer enforces that), and it emits a
decisive value only when confident; otherwise "unclear", which has no effect. A false
"absent"/"rarely" would cap a class at Poor, hence the conservative thresholds below.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from cqa.modules import ModuleResult
from cqa.scorer import GateSignal

LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "xc_logo.png"

# Speaker tile geometry (measured on 1280x720 frames: x >= ~1065, y <= ~120).
TILE_X0, TILE_Y1 = 0.82, 0.17
FULL_LOGO_FRAC = 0.4  # logo at least this wide (fraction of frame width) => full-frame speaker view
FULL_FACE_FRAC = 0.25
BLANK_STD = 4.0  # tile/frame with almost no variation (camera off) is not usable
# Logo matching.
LOGO_THRESHOLD = 0.45
LOGO_MIN_W = 90
LOGO_SCALES = 28
LOGO_FULL_THRESHOLD = 0.60  # stricter for wide regions (full frame), where spurious matches are likelier
WIDE_REGION = 400
# Gate thresholds (conservative by design).
MIN_FRAMES = 30
LOGO_PRESENT_RATE = 0.20
ABSENT_MIN_FACED_NONLOGO = 10
FACE_RARELY_RATE = 0.10
FACE_MOSTLY_RATE = 0.60

# Ruling: 'absent' is opt-in. Calibration on local frames showed 0 logo hits for classes that DID keep
# the background (B435g, B482: tile rarely on screen) and kids' faces satisfy the 'faced non-logo' test,
# so a default 'absent' could falsely cap a class at Poor.
ALLOW_ABSENT_DEFAULT = False
# 'rarely' is opt-in too: the top-right crop is assumed to be the speaker tile; another Zoom layout would
# yield faceless crops and could falsely cap a class. Seen layouts so far are too few to trust it by default.
ALLOW_RARELY_DEFAULT = False

FaceDetector = Callable[[np.ndarray], int]
_TEMPLATE: np.ndarray | None = None
_CASCADE = None
_CASCADES: list | None = None


def _template() -> np.ndarray:
    global _TEMPLATE
    if _TEMPLATE is None:
        img = cv2.imread(str(LOGO_PATH), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(LOGO_PATH)
        _TEMPLATE = img
    return _TEMPLATE


def _cascades() -> list:
    global _CASCADE, _CASCADES
    if _CASCADES is None:
        d = Path(cv2.data.haarcascades)
        _CASCADES = [
            cv2.CascadeClassifier(str(d / n))
            for n in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml", "haarcascade_profileface.xml")
        ]
        _CASCADE = _CASCADES[0]
    return _CASCADES


def haar_faces(region: np.ndarray) -> int:
    """Count faces (frontal x2 + profile Haar cascades). Small tiles are upscaled 4x with relaxed
    settings: tile faces are tiny and often partly hidden by the name label, and a missed face feeds
    the conservative "rarely" gate, so recall matters more than precision here."""
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
    if gray.shape[0] < 300:
        gray = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        sf, mn, ms = 1.05, 3, 40
    else:
        f = min(1.0, 480 / gray.shape[0])
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        sf, mn, ms = 1.1, 5, 40
    for c in _cascades():
        n = len(c.detectMultiScale(gray, scaleFactor=sf, minNeighbors=mn, minSize=(ms, ms)))
        if n:
            return n
    return 0


def _large_face(img: np.ndarray) -> bool:
    """A face at least FULL_FACE_FRAC of the frame height means a full-frame speaker view."""
    _cascades()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    f = 360 / gray.shape[0]
    gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    m = int(360 * FULL_FACE_FRAC)
    return len(_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(m, m))) > 0


def speaker_region(img: np.ndarray) -> tuple[np.ndarray, str]:
    """Return (region, kind). "full": full-frame speaker view (a frame-wide logo or a large face);
    otherwise "screenshare" with the top-right active-speaker tile."""
    h, w = img.shape[:2]
    small = cv2.resize(img, (640, max(1, int(h * 640 / w))), interpolation=cv2.INTER_AREA) if w > 640 else img
    if find_logo(small, min_w=int(FULL_LOGO_FRAC * small.shape[1])) or _large_face(img):
        return img, "full"
    return img[: int(TILE_Y1 * h), int(TILE_X0 * w) :], "screenshare"


def find_logo(region: np.ndarray, min_w: int | None = None) -> bool:
    """Multi-scale normalised template match of the 8x8 logo inside `region`."""
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
    if gray.std() < 2.0:  # flat region: nothing to match
        return False
    wide = gray.shape[1] > WIDE_REGION
    thr = LOGO_FULL_THRESHOLD if wide else LOGO_THRESHOLD
    if min_w is None:
        min_w = int(0.15 * gray.shape[1]) if wide else LOGO_MIN_W
    tpl = _template()
    max_w = min(int(gray.shape[1] * 0.95), 900)
    if max_w < min_w:
        return False
    for tw in np.linspace(min_w, max_w, LOGO_SCALES):
        tw = int(tw)
        th = max(8, int(tpl.shape[0] * tw / tpl.shape[1]))
        if th >= gray.shape[0] or tw >= gray.shape[1]:
            continue
        t = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        if t.std() < 2.0:
            continue
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        if np.nanmax(res) >= thr:
            return True
    return False


def _unclear(n: int) -> list[GateSignal]:
    return [
        GateSignal(module="xc", field="branded_background", value="unclear", n_frames=n),
        GateSignal(module="xc", field="face_visible", value="unclear", n_frames=n),
    ]


def analyze_frames(
    frames: list[tuple[float, np.ndarray]],
    face_detector: FaceDetector | None = None,
    allow_absent: bool = ALLOW_ABSENT_DEFAULT,
    allow_rarely: bool = ALLOW_RARELY_DEFAULT,
) -> ModuleResult:
    detect = face_detector or haar_faces
    kinds: Counter = Counter()
    logo_ts: list[float] = []
    faced = faced_nonlogo = 0
    for t, img in frames:
        if img is None:
            continue
        region, kind = speaker_region(img)
        if float(region.std()) < BLANK_STD:
            kinds["blank"] += 1
            continue
        kinds[kind] += 1
        logo = find_logo(region)
        face = detect(region) > 0
        if logo:
            logo_ts.append(t)
        faced += face
        faced_nonlogo += face and not logo
    n = sum(v for k, v in kinds.items() if k != "blank")
    if n == 0:
        return ModuleResult(
            module="xc", gates=_unclear(0),
            features={"frame_types": dict(kinds), "logo_hit_rate": 0.0, "face_hit_rate": 0.0},
        )

    logo_rate, face_rate = len(logo_ts) / n, faced / n
    if logo_rate >= LOGO_PRESENT_RATE:
        bg = "present"
    elif allow_absent and not logo_ts and n >= MIN_FRAMES and faced_nonlogo >= ABSENT_MIN_FACED_NONLOGO:
        bg = "absent"
    else:
        bg = "unclear"
    if n < MIN_FRAMES:
        fv = "unclear"
    elif face_rate < FACE_RARELY_RATE:
        fv = "rarely" if allow_rarely else "unclear"
    elif face_rate >= FACE_MOSTLY_RATE:
        fv = "mostly"
    else:
        fv = "unclear"
    return ModuleResult(
        module="xc",
        gates=[
            GateSignal(module="xc", field="branded_background", value=bg, n_frames=n),
            GateSignal(module="xc", field="face_visible", value=fv, n_frames=n),
        ],
        features={
            "frame_types": dict(kinds),
            "n_frames": n,
            "logo_hit_rate": round(logo_rate, 4),
            "face_hit_rate": round(face_rate, 4),
            "faced_nonlogo_frames": faced_nonlogo,
            "first_logo_t": logo_ts[0] if logo_ts else None,
            "last_logo_t": logo_ts[-1] if logo_ts else None,
        },
    )


def _duration(media: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(media)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def sample_frames(media_path: Path | str, n: int = 120, out_dir: Path | str | None = None) -> list[tuple[float, Path]]:
    """n evenly spaced frames (scaled to 1280 wide) written to out_dir; caller deletes."""
    media, out = Path(media_path), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = _duration(media)
    frames = []
    for i in range(n):
        t = round((i + 0.5) * d / n, 3)
        p = out / f"f{i:04d}.jpg"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t}", "-i", str(media), "-frames:v", "1",
             "-vf", "scale=1280:-2", str(p)],
            check=True,
        )
        if p.exists():
            frames.append((t, p))
    return frames


def crosscheck(media_path: Path | str, n: int = 120, face_detector: FaceDetector | None = None) -> ModuleResult:
    """Sample, analyze, and always delete the temporary frames."""
    tmp = Path(tempfile.mkdtemp(prefix="cqa_xc_"))
    try:
        sampled = sample_frames(media_path, n=n, out_dir=tmp)
        imgs = [(t, cv2.imread(str(p))) for t, p in sampled]
        kw = {"face_detector": face_detector} if face_detector else {}
        return analyze_frames(imgs, **kw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
