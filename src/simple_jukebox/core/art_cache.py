"""On-disk cache of album-cover thumbnails for the album grid.

Pulling a cover out of a FLAC/MP3 and scaling it is quick once, but the
grid shows hundreds of albums, so each thumbnail is saved after the first
time. The cache key includes the modification times of the track and of
its folder, so retagging a file, or dropping a cover.jpg into the album
folder, makes the grid pick up the new art.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from platformdirs import user_cache_dir

NO_ART_SUFFIX = ".none"  # remembered "this album has no art", so it isn't re-searched


def default_cache_dir() -> Path:
    return Path(user_cache_dir("Simple-Jukebox")) / "album-art"


def cache_key(track_path: str) -> Optional[str]:
    """None if the track can't be found (nothing worth caching)."""
    try:
        track_mtime = os.stat(track_path).st_mtime_ns
        folder_mtime = os.stat(os.path.dirname(track_path) or ".").st_mtime_ns
    except OSError:
        return None
    raw = f"{track_path}|{track_mtime}|{folder_mtime}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def thumbnail_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / key[:2] / f"{key}.jpg"


def no_art_marker(cache_dir: Path, key: str) -> Path:
    return cache_dir / key[:2] / f"{key}{NO_ART_SUFFIX}"
