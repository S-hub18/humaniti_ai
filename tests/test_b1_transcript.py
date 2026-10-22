import json
import subprocess
from types import SimpleNamespace

import pytest

from cqa.modules import b1_transcript as b1
from cqa.modules.b1_transcript import (
    B1_ITEMS,
    LeakError,
    judge,
    leak_check,
    plan_chunks,
    pseudonymise,
    run_b1,
    stitch,
    transcribe_audio,
)
from cqa.rubric import Status
from cqa.schema import TranscriptTurn
from cqa.transcript import parse_vtt

VTT = """WEBVTT

1
00:00:05.000 --> 00:00:09.000
Meera Rao: Hello Aditya, how was your day?

2
00:00:09.500 --> 00:00:11.000
Aditya K: Good ma'am!

3
00:00:12.000 --> 00:00:30.000
Audio shared by Meera Rao: Today we learn the opposition.

4
00:01:00.000 --> 00:01:08.000
Meera Rao: Riya, what is the best move here? Very good Riya!

5
00:01:09.000 --> 00:01:10.000
Riya's iPhone: Rook to h1.

6
00:40:00.000 --> 00:40:09.000
Meera Rao: For homework, solve these five puzzles before Monday.
"""


def turns():
    return parse_vtt(VTT)


# ---------- chunking / stitching ----------
def test_plan_chunks():
    assert plan_chunks(1500.0, 600.0, 30.0) == [(0.0, 600.0), (570.0, 1170.0), (1140.0, 1500.0)]
    assert plan_chunks(200.0, 600.0, 30.0) == [(0.0, 200.0)]


def test_stitch_absolute_and_dedupes_overlap():
    c0 = [TranscriptTurn(start=10, end=12, role="teacher", text="a"), TranscriptTurn(start=580, end=590, role="kid", text="dup")]
    c1 = [TranscriptTurn(start=10, end=20, role="kid", text="dup"), TranscriptTurn(start=40, end=45, role="teacher", text="b")]
    out = stitch([(0.0, 600.0), (570.0, 1170.0)], [c0, c1])
    texts = [(t.start, t.text) for t in out]
    assert texts == [(10, "a"), (580, "dup"), (610, "b")]  # chunk-1 copy of "dup" (abs 580) dropped


# ---------- pseudonymisation ----------
def test_pseudonymise_vtt_speakers_and_text():
    pt, mapping = pseudonymise(turns())
    assert [t.speaker for t in pt] == ["Teacher", "Kid1", "Video", "Teacher", "Kid2", "Teacher"]
    joined = " ".join(t.text for t in pt)
    for n in ("Meera", "Aditya", "Riya"):
        assert n not in joined
    assert "Kid1" in pt[0].text and "Kid2" in pt[3].text
    assert mapping["kids"] == 2


def test_pseudonymise_is_stable():
    a, _ = pseudonymise(turns())
    b, _ = pseudonymise(turns())
    assert [t.text for t in a] == [t.text for t in b]


def test_leak_check_fails_closed():
    bad = [TranscriptTurn(start=0, end=1, role="teacher", text="Well done Riya", speaker="Teacher")]
    with pytest.raises(LeakError):
        leak_check(bad, {"riya"})
    leak_check([TranscriptTurn(start=0, end=1, role="teacher", text="Well done Kid2", speaker="Teacher")], {"riya"})


def test_gemini_speaker_hints_are_pseudonymised():
    raw = [TranscriptTurn(start=0, end=2, role="teacher", text="Hi Kabir!", speaker="teacher"),
           TranscriptTurn(start=3, end=4, role="kid", text="Hello", speaker="Kabir")]
    pt, m = pseudonymise(raw)
    assert pt[1].speaker == "Kid1" and "Kabir" not in pt[0].text


# ---------- fake Gemini ----------
class FakeFiles:
    def __init__(self):
        self.uploaded, self.deleted = [], []

    def upload(self, file, config=None):
        self.uploaded.append(file)
        return SimpleNamespace(name=f"files/{len(self.uploaded)}", uri="u", mime_type="audio/mp4", state=SimpleNamespace(name="ACTIVE"))

    def get(self, name):
        return SimpleNamespace(name=name, uri="u", mime_type="audio/mp4", state=SimpleNamespace(name="ACTIVE"))

    def delete(self, name):
        self.deleted.append(name)


class FakeModels:
    def __init__(self, responder):
        self.responder, self.requests = responder, []

    def generate_content(self, model, contents, config):
        self.requests.append(SimpleNamespace(model=model, contents=contents, config=config))
        text = self.responder(len(self.requests), contents)
        return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=100, thoughts_token_count=0))


def client(responder):
    return SimpleNamespace(files=FakeFiles(), models=FakeModels(responder))


def _judge_payload(quote_ok="Kid2, what is the best move here?"):
    items = {i: {"status": "not_observable", "evidence": [], "rationale": "r"} for i in B1_ITEMS}
    items["individual_calling"] = {"status": "met", "rationale": "r",
                                  "evidence": [{"t_start": "01:00", "quote": quote_ok, "observation": "calls a child by name"}]}
    items["homework"] = {"status": "met", "rationale": "r",
                        "evidence": [{"t_start": "40:00", "quote": "For homework, solve these five puzzles before Monday.", "observation": "assigns"}]}
    items["rapport_intro"] = {"status": "met", "rationale": "r",
                             "evidence": [{"t_start": "00:05", "quote": "Tell me about your weekend trip to the zoo", "observation": "fabricated"}]}
    return json.dumps({"items": items})


def test_judge_drops_unverifiable_quotes_and_never_sends_names():
    pt, _ = pseudonymise(turns())
    c = client(lambda n, contents: _judge_payload())
    res = judge(pt, c, model="gemini-test-flash", duration=2700.0, sleep=lambda s: None)
    by_item = {v.item: v for v in res.votes}
    assert by_item["individual_calling"].status == Status.MET and by_item["homework"].status == Status.MET
    assert "rapport_intro" not in by_item  # fabricated quote dropped -> not observable -> no vote
    assert "camera_background" not in B1_ITEMS
    assert all(v.module == "b1" for v in res.votes)
    sent = " ".join(str(x) for x in c.models.requests[0].contents)
    for n in ("Meera", "Aditya", "Riya"):
        assert n not in sent
    assert "unsupported_claim:rapport_intro" in res.flags


def test_judge_retries_on_429():
    calls = {"n": 0}

    class RL(Exception):
        code = 429

    def responder(n, contents):
        if n == 1:
            raise RL("429 RESOURCE_EXHAUSTED retryDelay': '3s'")
        return _judge_payload()

    sleeps = []
    pt, _ = pseudonymise(turns())
    res = judge(pt, client(responder), model="gemini-test-flash", duration=2700.0, sleep=sleeps.append)
    assert res.ok and sleeps and sleeps[0] == pytest.approx(5.0)


@pytest.fixture
def audio_clip(tmp_path):
    p = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=25:size=320x240:rate=5",
                    "-f", "lavfi", "-i", "sine=duration=25", "-shortest", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def test_transcribe_audio_chunks_uploads_and_deletes(audio_clip):
    def responder(n, contents):
        return json.dumps({"turns": [{"start": "00:02", "end": "00:04", "role": "teacher", "speaker_hint": "teacher", "text": f"chunk {n}"}]})

    c = client(responder)
    out = transcribe_audio(audio_clip, c, model="gemini-test-flash", duration=25.0, chunk_s=10.0, overlap_s=2.0, sleep=lambda s: None)
    assert len(c.files.uploaded) == 3 and sorted(c.files.deleted) == sorted(f"files/{i}" for i in (1, 2, 3))
    assert [round(t.start) for t in out] == [2, 10, 18]  # chunk-relative -> absolute
    assert all(not __import__("pathlib").Path(f).exists() for f in c.files.uploaded)  # local chunk files removed


def test_run_b1_with_vtt_skips_transcription(tmp_path, audio_clip):
    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    c = client(lambda n, contents: _judge_payload())
    res = run_b1(audio_clip, vtt_path=vtt, client=c, model="gemini-test-flash", duration=2700.0, sleep=lambda s: None)
    assert res.ok and res.features["source"] == "vtt" and res.features["leak_check"] == "passed"
    assert len(c.models.requests) == 1 and not c.files.uploaded


def test_run_b1_fails_closed_on_leak(tmp_path, audio_clip, monkeypatch):
    vtt = tmp_path / "t.vtt"
    vtt.write_text(VTT)
    monkeypatch.setattr(b1, "pseudonymise", lambda t, extra_names=(): (t, {"kids": 0, "names": {"riya"}}))
    c = client(lambda n, contents: _judge_payload())
    res = run_b1(audio_clip, vtt_path=vtt, client=c, model="gemini-test-flash", duration=2700.0, sleep=lambda s: None)
    assert not res.ok and res.votes == [] and "leak" in res.error and not c.models.requests
