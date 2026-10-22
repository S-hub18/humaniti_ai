"""Disk cache for observer responses. Makes re-scoring, LOO and ablation free and reproducible.

Cache files live under .scratch/ (gitignored): responses describe real classes.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cqa.media import MediaRef
from cqa.observer import CallSpec, CallStats, Observer
from cqa.schema import ObserverOutput


class CachedObserver:
    def __init__(self, inner: Observer, root: Path | str, model: str, prompt_version: str) -> None:
        self.inner = inner
        self.root = Path(root)
        self.model = model
        self.prompt_version = prompt_version

    @contextmanager
    def session(self, media: MediaRef) -> Iterator[None]:
        inner_session = getattr(self.inner, "session", None)
        if inner_session is None:
            yield
        else:
            with inner_session(media):
                yield

    def key(self, media: MediaRef, spec: CallSpec, sample_idx: int) -> str:
        return "|".join(
            [media.sha256, spec.name, f"{spec.start:.1f}", f"{spec.end:.1f}", spec.media_resolution,
             self.prompt_version, self.model, str(sample_idx)]
        )

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def observe(self, media: MediaRef, spec: CallSpec, sample_idx: int) -> tuple[ObserverOutput, CallStats]:
        key = self.key(media, spec, sample_idx)
        path = self._path(key)
        if path.exists():
            data = json.loads(path.read_text())
            stats = CallStats.model_validate(data["stats"]).model_copy(update={"cached": True})
            return ObserverOutput.model_validate(data["output"]), stats
        out, stats = self.inner.observe(media, spec, sample_idx)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "output": out.model_dump(mode="json"), "stats": stats.model_dump()}))
        tmp.replace(path)
        return out, stats
