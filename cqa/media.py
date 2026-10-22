"""Media ingest: duration and a fast content hash (used as the cache key)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from pydantic import BaseModel

HASH_CHUNK = 8 * 1024 * 1024


class MediaRef(BaseModel):
    path: Path
    sha256: str
    duration: float


def fast_hash(path: Path) -> str:
    """sha256 over size + first and last 8 MB (full hashing of 300 MB uploads is needlessly slow)."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(HASH_CHUNK))
        if size > HASH_CHUNK:
            f.seek(max(HASH_CHUNK, size - HASH_CHUNK))
            h.update(f.read(HASH_CHUNK))
    return h.hexdigest()


def probe(path: Path | str) -> MediaRef:
    path = Path(path)
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    duration = float(json.loads(res.stdout)["format"]["duration"])
    return MediaRef(path=path, sha256=fast_hash(path), duration=duration)
