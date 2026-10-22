import json
from types import SimpleNamespace

import pytest

from cqa.gemini_observer import GeminiObserver
from cqa.media import MediaRef
from cqa.observer import CallSpec
from cqa.prompts import PROMPT_VERSION, build_prompt
from cqa.rubric import CALL_ITEMS, Status

MEDIA = MediaRef(path="class.mp4", sha256="abc", duration=3000.0)
CAM = {"branded_background": "present", "face_visible": "mostly", "camera_on_while_teaching": "mostly", "lighting": "ok"}


def payload(call, t="45:10"):
    items = {i: {"status": "met", "evidence": [{"t_start": t, "modality": "audio", "observation": "o"}], "rationale": "r"} for i in CALL_ITEMS[call]}
    body = {"items": items, "camera": CAM}
    if call == "main":
        body.update(session_type="regular_class", class_start="00:21", class_end="49:00")
    return json.dumps(body)


class FakeFiles:
    def __init__(self, states=("PROCESSING", "ACTIVE")):
        self.states = list(states)
        self.deleted = []
        self.uploaded = 0

    def upload(self, file, config=None):
        self.uploaded += 1
        return SimpleNamespace(name="files/1", uri="https://x/files/1", mime_type="video/mp4", state=SimpleNamespace(name=self.states[0]))

    def get(self, name):
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SimpleNamespace(name=name, uri="https://x/files/1", mime_type="video/mp4", state=SimpleNamespace(name=st))

    def delete(self, name):
        self.deleted.append(name)


class FakeModels:
    def __init__(self, texts, fail=None):
        self.texts = list(texts)
        self.requests = []
        self.fail = fail

    def generate_content(self, model, contents, config):
        self.requests.append(SimpleNamespace(model=model, contents=contents, config=config))
        if self.fail:
            raise self.fail
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        usage = SimpleNamespace(prompt_token_count=1_000_000, candidates_token_count=1000, thoughts_token_count=1000)
        return SimpleNamespace(text=text, usage_metadata=usage)


def make(texts, fail=None, states=("PROCESSING", "ACTIVE")):
    client = SimpleNamespace(files=FakeFiles(states), models=FakeModels(texts, fail))
    obs = GeminiObserver(client, model="gemini-test-flash", prices={"input_per_m": 2.0, "output_per_m": 10.0}, poll_s=0)
    return obs, client


def test_request_has_schema_offsets_and_resolution():
    obs, client = make([payload("closing")])
    spec = CallSpec(name="closing", start=2400.0, end=3000.0, media_resolution="high")
    with obs.session(MEDIA):
        out, stats = obs.observe(MEDIA, spec, 0)
    req = client.models.requests[0]
    part = req.contents[0]
    assert part.video_metadata.start_offset == "2400s" and part.video_metadata.end_offset == "3000s"
    assert part.video_metadata.fps == 1
    assert req.config.temperature == 0.4
    assert req.config.response_mime_type == "application/json"
    assert set(req.config.response_json_schema["properties"]["items"]["properties"]) == set(CALL_ITEMS["closing"])
    assert req.config.media_resolution.name == "MEDIA_RESOLUTION_HIGH"
    assert out.call == "closing" and out.window == (2400.0, 3000.0)
    assert out.items["homework"].status == Status.MET
    assert client.files.deleted == ["files/1"]


def test_upload_once_per_session_and_deleted_on_error():
    obs, client = make([payload("main")], fail=RuntimeError("boom"))
    spec = CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low")
    with pytest.raises(RuntimeError):
        with obs.session(MEDIA):
            obs.observe(MEDIA, spec, 0)
    assert client.files.uploaded == 1 and client.files.deleted == ["files/1"]


def test_observe_without_session_uploads_and_deletes():
    obs, client = make([payload("main")])
    obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)
    assert client.files.uploaded == 1 and client.files.deleted == ["files/1"]


def test_parse_failure_retries_then_not_observable():
    obs, client = make(["not json", "still not json"])
    spec = CallSpec(name="opening", start=0.0, end=600.0, media_resolution="high")
    with obs.session(MEDIA):
        out, _ = obs.observe(MEDIA, spec, 0)
    assert len(client.models.requests) == 2
    assert all(i.status == Status.NOT_OBSERVABLE for i in out.items.values())
    assert set(out.items) == set(CALL_ITEMS["opening"])


def test_parse_retry_recovers():
    obs, client = make(["garbage", payload("opening", t="02:00")])
    with obs.session(MEDIA):
        out, _ = obs.observe(MEDIA, CallSpec(name="opening", start=0.0, end=600.0, media_resolution="high"), 0)
    assert out.items["rapport_intro"].status == Status.MET


def test_stats_from_usage_metadata():
    obs, _ = make([payload("main")])
    with obs.session(MEDIA):
        _, stats = obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)
    assert stats.input_tokens == 1_000_000 and stats.output_tokens == 2000
    assert stats.cost_usd == pytest.approx(2.0 + 0.02)


def test_clip_relative_timestamps_are_shifted():
    # model answered with clip-relative times (02:00) for a window starting at 40:00
    obs, _ = make([payload("closing", t="02:00")])
    with obs.session(MEDIA):
        out, _ = obs.observe(MEDIA, CallSpec(name="closing", start=2400.0, end=3000.0, media_resolution="high"), 0)
    assert out.items["homework"].evidence[0].t_start == "42:00"


def test_failed_processing_raises_and_deletes():
    obs, client = make([payload("main")], states=("PROCESSING", "FAILED"))
    with pytest.raises(RuntimeError):
        with obs.session(MEDIA):
            obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)
    assert client.files.deleted == ["files/1"]


def test_session_without_calls_never_uploads():
    obs, client = make([payload("main")])
    with obs.session(MEDIA):
        pass
    assert client.files.uploaded == 0 and client.files.deleted == []


def test_one_upload_for_many_calls():
    obs, client = make([payload("main")])
    spec = CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low")
    with obs.session(MEDIA):
        for k in range(3):
            obs.observe(MEDIA, spec, k)
    assert client.files.uploaded == 1 and client.files.deleted == ["files/1"]


def test_prompt_contains_rules_and_version():
    p = build_prompt(CallSpec(name="opening", start=120.0, end=720.0, media_resolution="high"), 3000.0)
    assert "never name" in p.lower() and "02:00" in p and "12:00" in p
    assert "rapport_intro" in p and "homework" not in p
    assert PROMPT_VERSION


def test_large_file_uploads_a_proxy_and_cleans_it(tmp_path):
    import subprocess as sp

    clip = tmp_path / "big.mp4"
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=10",
            "-f", "lavfi", "-i", "sine=duration=4", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
    obs, client = make([payload("main")])
    obs.proxy_above_bytes = 1  # force proxy
    uploaded = []
    orig = client.files.upload

    def spy(file, config=None):
        uploaded.append(file)
        assert Path(file).exists() and Path(file).name == "proxy.mp4"
        return orig(file, config)

    client.files.upload = spy
    from pathlib import Path
    media = MediaRef(path=clip, sha256="abc", duration=4.0)
    obs.observe(media, CallSpec(name="main", start=0.0, end=4.0, media_resolution="low"), 0)
    assert len(uploaded) == 1 and not Path(uploaded[0]).exists()  # proxy deleted after upload
    assert client.files.deleted == ["files/1"]


class RateLimited(Exception):
    code = 429


def test_429_retries_with_api_delay():
    obs, client = make([payload("main")])
    sleeps = []
    obs._sleep = sleeps.append
    real = client.models.generate_content
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("429 RESOURCE_EXHAUSTED ... 'retryDelay': '54s'")
        return real(**kw)

    client.models.generate_content = flaky
    out, _ = obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)
    assert out.items["homework"].status == Status.MET
    assert len(sleeps) == 2 and sleeps[0] == pytest.approx(56.0) and sleeps[1] > sleeps[0]


def test_429_gives_up_after_max_retries():
    obs, client = make([payload("main")])
    obs._sleep = lambda s: None
    obs.max_retries = 2

    def always(**kw):
        raise RateLimited("429 RESOURCE_EXHAUSTED Please retry in 3.5s")

    client.models.generate_content = always
    with pytest.raises(RateLimited):
        obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)
    assert client.files.deleted == ["files/1"]


def test_non_429_error_not_retried():
    obs, client = make([payload("main")], fail=ValueError("bad request"))
    obs._sleep = lambda s: pytest.fail("should not sleep")
    with pytest.raises(ValueError):
        obs.observe(MEDIA, CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low"), 0)


def test_min_call_interval_spaces_calls():
    obs, _ = make([payload("main")])
    sleeps = []
    obs._sleep = sleeps.append
    obs.min_call_interval_s = 65.0
    spec = CallSpec(name="main", start=0.0, end=3000.0, media_resolution="low")
    with obs.session(MEDIA):
        obs.observe(MEDIA, spec, 0)
        obs.observe(MEDIA, spec, 1)
    assert len(sleeps) == 1 and 60.0 < sleeps[0] <= 65.0
