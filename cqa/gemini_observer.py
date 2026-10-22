"""Real Gemini observer (google-genai). Uploads the recording once per session and always deletes it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

import logging

from google.genai import types
from pydantic import ValidationError

from cqa.config import model_id, prices_for
from cqa.evidence import WINDOW_TOLERANCE_S, fmt_ts, parse_ts
from cqa.media import MediaRef
from cqa.observer import CallSpec, CallStats
from cqa.prompts import build_prompt
from cqa.rubric import CALL_ITEMS, Status
from cqa.schema import ItemObservation, ObserverOutput, gemini_response_schema


log = logging.getLogger(__name__)

RESOLUTION = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}


class GeminiObserver:
    def __init__(
        self,
        client,
        model: str | None = None,
        temperature: float = 0.4,
        prices: dict[str, float] | None = None,
        fps: float = 1.0,
        poll_s: float = 5.0,
        max_wait_s: float = 900.0,
        proxy_above_bytes: int = 40 * 1024 * 1024,
        max_retries: int = 4,
        min_call_interval_s: float = 0.0,
        sleep=time.sleep,
    ) -> None:
        self.proxy_above_bytes = proxy_above_bytes
        self.max_retries = max_retries
        self.min_call_interval_s = min_call_interval_s
        self._sleep = sleep
        self._last_call: float | None = None
        self.client = client
        self.model = model_id(model)  # Flash / Flash-Lite only; raises on Pro
        self.temperature = temperature
        self.prices = prices or prices_for(self.model)
        self.fps = fps
        self.poll_s = poll_s
        self.max_wait_s = max_wait_s
        self._file = None
        self._file_sha: str | None = None
        self._session_sha: str | None = None

    # ---- upload lifecycle -------------------------------------------------
    def _proxy(self, media: MediaRef, tmpdir: Path) -> Path:
        """Small same-duration proxy (480p, 2 fps, mono audio) so large recordings upload reliably.

        Gemini samples ~1 fps and low media resolution anyway, so this loses little signal; timestamps are unchanged.
        """
        src = Path(media.path)
        if not src.exists() or src.stat().st_size <= self.proxy_above_bytes:
            return src
        out = tmpdir / "proxy.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(media.path), "-vf", "scale=-2:480,fps=2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-ac", "1", "-b:a", "48k",
             str(out)],
            check=True,
        )
        return out

    def _upload(self, media: MediaRef):
        tmpdir = Path(tempfile.mkdtemp(prefix="cqa_proxy_"))
        try:
            src = self._proxy(media, tmpdir)
            f = self.client.files.upload(file=str(src), config={"mime_type": "video/mp4"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            waited = 0.0
            while f.state.name == "PROCESSING":
                if waited > self.max_wait_s:
                    raise RuntimeError("Gemini file processing timed out")
                time.sleep(self.poll_s)
                waited += max(self.poll_s, 1e-3)
                f = self.client.files.get(name=f.name)
            if f.state.name != "ACTIVE":
                raise RuntimeError(f"Gemini file processing failed: {f.state.name}")
            return f
        except BaseException:
            self.client.files.delete(name=f.name)
            raise

    @contextmanager
    def session(self, media: MediaRef) -> Iterator[None]:
        """Lazy session: upload on the first uncached call, reuse for all calls, delete in `finally`."""
        self._session_sha = media.sha256
        try:
            yield
        finally:
            f = self._file
            self._file, self._file_sha, self._session_sha = None, None, None
            if f is not None:
                self.client.files.delete(name=f.name)

    # ---- calls ------------------------------------------------------------
    def _request(self, f, spec: CallSpec, duration: float):
        video = types.Part(
            file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type),
            video_metadata=types.VideoMetadata(
                start_offset=f"{int(spec.start)}s", end_offset=f"{int(round(spec.end))}s", fps=self.fps
            ),
        )
        config = types.GenerateContentConfig(
            temperature=self.temperature,
            response_mime_type="application/json",
            response_json_schema=gemini_response_schema(spec.name),
            media_resolution=RESOLUTION[spec.media_resolution],
        )
        return self.client.models.generate_content(
            model=self.model, contents=[video, build_prompt(spec, duration)], config=config
        )

    @staticmethod
    def _retry_delay(exc: Exception) -> float | None:
        """Seconds to wait if `exc` is a 429 / RESOURCE_EXHAUSTED, using the API's retryDelay when present."""
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        text = str(exc)
        if code != 429 and "RESOURCE_EXHAUSTED" not in text:
            return None
        m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", text) or re.search(r"retry in (\d+(?:\.\d+)?)s", text)
        return float(m.group(1)) if m else 30.0

    def _request_with_retry(self, f, spec: CallSpec, duration: float):
        if self._last_call is not None and self.min_call_interval_s > 0:
            wait = self.min_call_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._request(f, spec, duration)
                self._last_call = time.monotonic()
                return resp
            except Exception as exc:
                delay = self._retry_delay(exc)
                if delay is None or attempt == self.max_retries:
                    raise
                wait = min(delay + 2.0, 120.0) * (1 + attempt * 0.5)
                log.warning("Gemini rate limited (429); retry %d/%d in %.0f s", attempt + 1, self.max_retries, wait)
                self._sleep(wait)
        raise RuntimeError("unreachable")

    def _stats(self, resp, latency: float) -> CallStats:
        u = resp.usage_metadata
        inp = int(getattr(u, "prompt_token_count", 0) or 0)
        out = int(getattr(u, "candidates_token_count", 0) or 0) + int(getattr(u, "thoughts_token_count", 0) or 0)
        cost = inp / 1e6 * self.prices["input_per_m"] + out / 1e6 * self.prices["output_per_m"]
        return CallStats(input_tokens=inp, output_tokens=out, latency_s=latency, cost_usd=cost)

    @staticmethod
    def _parse(text: str, spec: CallSpec) -> ObserverOutput:
        data = json.loads(text)
        data["call"] = spec.name
        data["window"] = (spec.start, spec.end)
        if spec.name != "main":
            for k in ("session_type", "class_start", "class_end"):
                data.pop(k, None)
        return ObserverOutput.model_validate(data)

    @staticmethod
    def _shift_clip_relative(out: ObserverOutput, spec: CallSpec) -> ObserverOutput:
        """If every timestamp looks clip-relative (fits the clip length, none inside the window), shift by start."""
        if spec.start <= 0:
            return out
        times = []
        for item in out.items.values():
            for ev in item.evidence:
                try:
                    times.append(parse_ts(ev.t_start))
                except ValueError:
                    pass
        length = spec.end - spec.start
        inside = [t for t in times if spec.start - WINDOW_TOLERANCE_S <= t <= spec.end + WINDOW_TOLERANCE_S]
        if not times or inside or max(times) > length + WINDOW_TOLERANCE_S:
            return out
        items = {}
        for k, item in out.items.items():
            evs = []
            for ev in item.evidence:
                try:
                    ev = ev.model_copy(update={"t_start": fmt_ts(parse_ts(ev.t_start) + spec.start)})
                except ValueError:
                    pass
                evs.append(ev)
            items[k] = item.model_copy(update={"evidence": evs})
        return out.model_copy(update={"items": items})

    def _empty(self, spec: CallSpec) -> ObserverOutput:
        return ObserverOutput(
            call=spec.name,
            window=(spec.start, spec.end),
            items={i: ItemObservation(status=Status.NOT_OBSERVABLE, rationale="model output unparseable") for i in CALL_ITEMS[spec.name]},
        )

    def observe(self, media: MediaRef, spec: CallSpec, sample_idx: int) -> tuple[ObserverOutput, CallStats]:
        if self._session_sha != media.sha256:
            with self.session(media):
                return self.observe(media, spec, sample_idx)
        if self._file is None:
            self._file, self._file_sha = self._upload(media), media.sha256
        total = CallStats()
        for _attempt in range(2):
            t0 = time.monotonic()
            resp = self._request_with_retry(self._file, spec, media.duration)
            st = self._stats(resp, time.monotonic() - t0)
            total = CallStats(
                input_tokens=total.input_tokens + st.input_tokens,
                output_tokens=total.output_tokens + st.output_tokens,
                latency_s=total.latency_s + st.latency_s,
                cost_usd=total.cost_usd + st.cost_usd,
            )
            try:
                return self._shift_clip_relative(self._parse(resp.text, spec), spec), total
            except (json.JSONDecodeError, ValidationError, TypeError):
                continue
        return self._empty(spec), total
