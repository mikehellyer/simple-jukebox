"""Reading a music file's tags into a plain TrackInfo.

Goes through mutagen's "easy" interface so MP3 (ID3), FLAC/Ogg (Vorbis
comments) and M4A (MP4 atoms) all come back with the same key names.
Anything missing falls back to something sensible from the filename and
folder layout (Artist/Album/NN Title.ext), so a badly tagged file still
lands somewhere findable in the library instead of under "Unknown".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mutagen

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav", ".wma", ".aiff", ".aif"}

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"

_LEADING_TRACK_NUMBER = re.compile(r"^\s*(\d{1,3})\s*[-._ ]+\s*(.+)$")


@dataclass
class TrackInfo:
    path: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str = ""
    year: Optional[int] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    duration: float = 0.0


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def _first(tags, key: str) -> str:
    if not tags:
        return ""
    try:
        values = tags.get(key)
    except (KeyError, ValueError):
        return ""
    if not values:
        return ""
    value = values[0] if isinstance(values, list) else values
    return str(value).strip()


def _leading_int(text: str) -> Optional[int]:
    """"3/12" → 3, "2004-05-01" → 2004, "" → None."""
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _guess_from_path(path: Path) -> tuple[str, Optional[int]]:
    stem = path.stem
    match = _LEADING_TRACK_NUMBER.match(stem)
    if match:
        return match.group(2).strip(), int(match.group(1))
    return stem, None


def read_track(path: Path) -> Optional[TrackInfo]:
    """Return the file's tags, or None if mutagen can't read it as audio."""
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:  # mutagen raises a zoo of format-specific errors
        return None
    if audio is None:
        return None

    tags = audio.tags
    guessed_title, guessed_number = _guess_from_path(path)
    folder_album = path.parent.name or UNKNOWN_ALBUM
    folder_artist = path.parent.parent.name if path.parent.parent != path.parent else ""

    artist = _first(tags, "artist") or _first(tags, "albumartist") or folder_artist or UNKNOWN_ARTIST
    album_artist = _first(tags, "albumartist") or artist
    duration = float(getattr(audio.info, "length", 0.0) or 0.0)

    return TrackInfo(
        path=str(path),
        title=_first(tags, "title") or guessed_title,
        artist=artist,
        album=_first(tags, "album") or folder_album,
        album_artist=album_artist,
        genre=_first(tags, "genre"),
        year=_leading_int(_first(tags, "date")),
        track_number=_leading_int(_first(tags, "tracknumber")) or guessed_number,
        disc_number=_leading_int(_first(tags, "discnumber")),
        duration=duration,
    )
