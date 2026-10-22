"""B2 audio module: local signal-processing features -> one `energy_warmth` vote.

No external APIs. Zoom noise-gates audio, so SNR / peak / clipping are uninformative;
we only use speech level and vocal liveliness (pitch range, loudness dynamics).
Silence is NOT penalised globally (silent lichess play is legitimate).
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from cqa.aggregate import ModuleVote
from cqa.modules import ModuleResult
from cqa.schema import Evidence, TranscriptTurn

# --- thresholds: provisional, calibrated on n<=2 local files ---
FRAME_S = 0.030
VAD_ABS_DB = -45.0  # frames quieter than this are never speech
VAD_REL_DB = 30.0  # ...nor frames this far below the file's p95 frame level
MIN_SPEECH_S = 60.0
LOW_LEVEL_P90_DB = -21.0  # provisional, calibrated on n<=2 local files
PITCH_HIGH_ST = 7.0  # provisional, calibrated on n<=2 local files
DYN_HIGH_DB = 6.0  # provisional, calibrated on n<=2 local files
PITCH_LOW_ST = 3.0  # provisional, calibrated on n<=2 local files
DYN_LOW_DB = 3.0  # provisional, calibrated on n<=2 local files
DEAD_AIR_GAP_S = 20.0
DROPOUT_MIN_RUN_S = 1.0
DROPOUT_MAX_RUN_S = 5.0
DROPOUT_DEAD_DB = -90.0
DROPOUT_MIN_COUNT = 5  # provisional
F0_MIN, F0_MAX = 70.0, 400.0
F0_WIN = 1024
F0_MAX_FRAMES = 8000
YIN_THRESH = 0.15
LIVELY_WINDOW_S = 10.0


def extract_audio(media_path, out_wav, sr: int = 16000) -> None:
    """Extract mono `sr` Hz wav via ffmpeg. Raises RuntimeError on failure."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(media_path), "-vn", "-ac", "1",
           "-ar", str(sr), "-c:a", "pcm_s16le", str(out_wav)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"ffmpeg unavailable: {e}") from e
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[:300]}")


def estimate_f0(frame: np.ndarray, sr: int) -> float | None:
    """YIN-style F0 (Hz) for one frame, or None if unvoiced."""
    x = np.asarray(frame, dtype=np.float64)
    x = x - x.mean()
    tau_min = int(sr / F0_MAX)
    tau_max = min(int(sr / F0_MIN), len(x) // 2 - 1)
    if tau_max <= tau_min + 2 or not np.any(x):
        return None
    n = len(x)
    nfft = 1 << (2 * n - 1).bit_length()
    f = np.fft.rfft(x, nfft)
    r = np.fft.irfft(f * np.conj(f), nfft)[: tau_max + 1]
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    taus = np.arange(1, tau_max + 1)
    w = n - taus  # overlap length
    e1 = cs[w]  # sum x[0:w]^2
    e2 = cs[n] - cs[taus]  # sum x[tau:n]^2
    d = e1 + e2 - 2 * r[1:]
    cm = np.cumsum(d)
    cmnd = d * taus / np.maximum(cm, 1e-12)
    seg = cmnd[tau_min - 1 : tau_max]  # taus tau_min..tau_max
    below = np.where(seg < YIN_THRESH)[0]
    if len(below) == 0:
        return None
    i = below[0]
    while i + 1 < len(seg) and seg[i + 1] < seg[i]:
        i += 1
    tau = float(i + tau_min)
    j = i + tau_min - 1  # index into cmnd
    if 0 < j < len(cmnd) - 1:
        a, b, c = cmnd[j - 1], cmnd[j], cmnd[j + 1]
        den = a - 2 * b + c
        if den > 1e-12:
            tau += 0.5 * (a - c) / den
    return float(sr / tau) if tau > 0 else None


def _read_wav(path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"expected 16-bit wav, got {sw * 8}-bit")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    return x, sr


def _mmss(t: float) -> str:
    t = int(max(t, 0))
    return f"{t // 60:02d}:{t % 60:02d}"


def _empty_features(teacher_only: bool) -> dict:
    return {
        "teacher_only": teacher_only, "speech_s": 0.0, "speech_ratio": 0.0,
        "speech_level_p90_db": None, "speech_level_median_db": None,
        "loudness_dynamics": None, "pitch_range_semitones": None, "pitch_cv": None,
        "long_dead_air_s": None, "dropout_count": 0, "insufficient_speech": True,
    }


def _gaps(speech_t: np.ndarray, lo: float, hi: float) -> list[float]:
    pts = np.concatenate([[lo], np.sort(speech_t), [hi]])
    g = np.diff(pts)
    return [float(v) for v in g if v > DEAD_AIR_GAP_S]


def analyze_audio(
    wav_path,
    teacher_turns: list[TranscriptTurn] | None = None,
    teaching_window: tuple[float, float] | None = None,
) -> ModuleResult:
    try:
        return _analyze(wav_path, teacher_turns, teaching_window)
    except Exception as e:  # never raise
        return ModuleResult(module="b2", ok=False, error=f"{type(e).__name__}: {e}")


def _analyze(wav_path, teacher_turns, teaching_window) -> ModuleResult:
    x, sr = _read_wav(wav_path)
    hop = max(int(FRAME_S * sr), 1)
    nf = len(x) // hop
    teacher_only = bool(teacher_turns)
    feats = _empty_features(teacher_only)
    if nf == 0:
        return ModuleResult(module="b2", features=feats)
    frames = x[: nf * hop].reshape(nf, hop)
    rms = np.sqrt((frames**2).mean(axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-6))  # floor -120 dBFS
    centers = (np.arange(nf) + 0.5) * hop / sr
    dur = len(x) / sr

    lo, hi = teaching_window if teaching_window else (0.0, dur)
    region = (centers >= lo) & (centers <= hi)
    if teacher_only:
        tmask = np.zeros(nf, dtype=bool)
        for t in teacher_turns:
            if t.role == "teacher":
                tmask |= (centers >= t.start) & (centers <= t.end)
        region &= tmask

    gate = max(VAD_ABS_DB, float(np.percentile(db, 95)) - VAD_REL_DB)
    is_speech = db > gate
    sp = is_speech & region
    n_sp = int(sp.sum())
    n_region = int(region.sum())
    speech_s = n_sp * FRAME_S
    feats["speech_s"] = round(speech_s, 1)
    feats["speech_ratio"] = round(n_sp / n_region, 3) if n_region else 0.0

    if teacher_only:
        wlo, whi = (lo, hi)
        feats["long_dead_air_s"] = round(sum(_gaps(centers[is_speech & (centers >= wlo) & (centers <= whi)], wlo, whi)), 1)

    # dropouts: 1-5 s digital-zero runs with speech right on both sides
    dead = db < DROPOUT_DEAD_DB
    drops, i = 0, 0
    min_f, max_f = int(DROPOUT_MIN_RUN_S / FRAME_S), int(DROPOUT_MAX_RUN_S / FRAME_S)
    while i < nf:
        if dead[i]:
            j = i
            while j < nf and dead[j]:
                j += 1
            if min_f <= j - i <= max_f and i > 0 and j < nf and is_speech[i - 1] and is_speech[j]:
                drops += 1
            i = j
        else:
            i += 1
    feats["dropout_count"] = drops

    flags: list[str] = []
    if drops >= DROPOUT_MIN_COUNT:
        flags.append("audio_defect:dropouts")

    if n_sp == 0 or speech_s < MIN_SPEECH_S:
        if n_sp:
            feats["speech_level_p90_db"] = round(float(np.percentile(db[sp], 90)), 2)
            feats["speech_level_median_db"] = round(float(np.median(db[sp])), 2)
        return ModuleResult(module="b2", flags=flags, features=feats)

    sdb = db[sp]
    p90 = float(np.percentile(sdb, 90))
    dyn = float(np.percentile(sdb, 75) - np.percentile(sdb, 25))
    feats.update(
        speech_level_p90_db=round(p90, 2),
        speech_level_median_db=round(float(np.median(sdb)), 2),
        loudness_dynamics=round(dyn, 2),
        insufficient_speech=False,
    )
    if p90 < LOW_LEVEL_P90_DB:
        flags.append("audio_defect:low_level")

    # F0 on (strided) speech frames
    idx = np.where(sp)[0]
    stride = max(1, len(idx) // F0_MAX_FRAMES)
    f_t, f_st = [], []
    half = F0_WIN // 2
    for k in idx[::stride]:
        c = int(centers[k] * sr)
        s = min(max(c - half, 0), max(len(x) - F0_WIN, 0))
        f0 = estimate_f0(x[s : s + F0_WIN], sr)
        if f0:
            f_t.append(centers[k])
            f_st.append(12 * np.log2(f0 / 100.0))
    f_t, f_st = np.array(f_t), np.array(f_st)
    pitch_range = 0.0
    if len(f_st) >= 10:
        pitch_range = float(np.percentile(f_st, 90) - np.percentile(f_st, 10))
        f_hz = 100.0 * 2 ** (f_st / 12)
        feats["pitch_cv"] = round(float(f_hz.std() / f_hz.mean()), 4)
    feats["pitch_range_semitones"] = round(pitch_range, 2)

    if pitch_range >= PITCH_HIGH_ST and dyn >= DYN_HIGH_DB:
        status = "met"
    elif pitch_range < PITCH_LOW_ST and dyn < DYN_LOW_DB:
        status = "not_met"
    else:
        status = "partial"

    # most lively 10 s window (pitch range + loudness IQR), hop 5 s
    best_t, best_s, best_r, best_d = lo, -1.0, pitch_range, dyn
    sp_t = centers[idx]
    t = lo
    while t < max(hi - LIVELY_WINDOW_S, lo) + 1e-9:
        m = (sp_t >= t) & (sp_t < t + LIVELY_WINDOW_S)
        if m.sum() * FRAME_S >= LIVELY_WINDOW_S * 0.3:
            wd = db[idx[m]]
            wdyn = float(np.percentile(wd, 75) - np.percentile(wd, 25))
            fm = (f_t >= t) & (f_t < t + LIVELY_WINDOW_S)
            wr = float(np.percentile(f_st[fm], 90) - np.percentile(f_st[fm], 10)) if fm.sum() >= 5 else 0.0
            if wr + wdyn > best_s:
                best_t, best_s, best_r, best_d = t, wr + wdyn, wr, wdyn
        t += 5.0
    ev = Evidence(
        t_start=_mmss(best_t), modality="audio",
        observation=f"vocal liveliness: pitch range {best_r:.1f} st, dynamics {best_d:.1f} dB",
    )
    vote = ModuleVote(module="b2", item="energy_warmth", status=status, weight=0.5, evidence=[ev])
    return ModuleResult(module="b2", votes=[vote], flags=flags, features=feats)
