import subprocess
import wave

import numpy as np

from cqa.modules.b2_audio import analyze_audio, estimate_f0, extract_audio
from cqa.schema import TranscriptTurn

SR = 16000


def write_wav(path, x, sr=SR):
    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def lively(dur=90.0):
    t = np.arange(int(dur * SR)) / SR
    f0 = 180 * 2 ** ((6 * np.sin(2 * np.pi * 0.4 * t)) / 12)  # +-6 st
    phase = 2 * np.pi * np.cumsum(f0) / SR
    amp = 0.05 + 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.23 * t))
    return amp * np.sin(phase)


def monotone(dur=90.0, amp=0.2):
    t = np.arange(int(dur * SR)) / SR
    return amp * np.sin(2 * np.pi * 150 * t)


def test_f0_recovers_sines():
    t = np.arange(int(0.05 * SR)) / SR
    for f in (150.0, 220.0):
        est = estimate_f0(np.sin(2 * np.pi * f * t), SR)
        assert est is not None and abs(est - f) / f < 0.05


def test_lively_is_met(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, lively())
    r = analyze_audio(p)
    assert r.ok and r.module == "b2"
    assert len(r.votes) == 1
    v = r.votes[0]
    assert (v.item, v.weight, v.status) == ("energy_warmth", 0.5, "met")
    assert v.evidence[0].modality == "audio"
    assert r.features["teacher_only"] is False


def test_monotone_is_not_met(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, monotone())
    r = analyze_audio(p)
    assert r.votes[0].status == "not_met"


def test_quiet_flags_low_level(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, monotone(amp=0.03))
    r = analyze_audio(p)
    assert "audio_defect:low_level" in r.flags
    p2 = tmp_path / "b.wav"
    write_wav(p2, monotone(amp=0.3))
    assert "audio_defect:low_level" not in analyze_audio(p2).flags


def test_silence_and_short_no_vote(tmp_path):
    p = tmp_path / "s.wav"
    write_wav(p, np.zeros(SR * 60))
    r = analyze_audio(p)
    assert r.ok and not r.votes and r.features["insufficient_speech"] is True
    p2 = tmp_path / "short.wav"
    write_wav(p2, lively(5.0))
    r2 = analyze_audio(p2)
    assert r2.ok and not r2.votes and r2.features["insufficient_speech"] is True


def test_teacher_turns_restrict(tmp_path):
    # 0-90 s lively, 90-180 s monotone
    p = tmp_path / "a.wav"
    write_wav(p, np.concatenate([lively(90), monotone(90)]))
    t_live = [TranscriptTurn(start=0, end=90, role="teacher", text="x")]
    t_mono = [TranscriptTurn(start=90, end=180, role="teacher", text="x")]
    a = analyze_audio(p, teacher_turns=t_live)
    b = analyze_audio(p, teacher_turns=t_mono)
    assert a.features["teacher_only"] is True
    assert a.features["pitch_range_semitones"] > b.features["pitch_range_semitones"]
    assert a.votes[0].status == "met" and b.votes[0].status == "not_met"


def test_dead_air(tmp_path):
    x = np.concatenate([monotone(70), np.zeros(SR * 30), monotone(10)])
    p = tmp_path / "a.wav"
    write_wav(p, x)
    turns = [TranscriptTurn(start=0, end=110, role="teacher", text="x")]
    r = analyze_audio(p, teacher_turns=turns, teaching_window=(0, 110))
    assert r.features["long_dead_air_s"] >= 25
    assert analyze_audio(p).features["long_dead_air_s"] is None


def test_extract_audio(tmp_path):
    src = tmp_path / "a.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=2", "-shortest", str(src)],
        check=True,
    )
    out = tmp_path / "o.wav"
    extract_audio(src, out)
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)


def test_bad_media_returns_not_ok(tmp_path):
    bad = tmp_path / "nope.wav"
    bad.write_bytes(b"garbage")
    r = analyze_audio(bad)
    assert not r.ok and r.error
