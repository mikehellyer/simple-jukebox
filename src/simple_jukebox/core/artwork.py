"""Finding album art for a track: embedded in the file first, then a
cover image sitting next to it in the album folder (cover.jpg,
folder.jpg, …) the way most rippers and Strawberry/foobar lay it out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import mutagen

FOLDER_ART_NAMES = ("cover", "folder", "front", "album", "albumart")
FOLDER_ART_EXTENSIONS = (".jpg", ".jpeg", ".png")


def embedded_art(path: Path) -> Optional[bytes]:
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return None
    if audio is None:
        return None

    # FLAC keeps pictures in their own block, not in the tags.
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data

    tags = audio.tags
    if not tags:
        return None
    for key in tags.keys():
        # ID3: APIC:<description>
        if str(key).startswith("APIC"):
            return tags[key].data
    # MP4: covr
    if "covr" in tags and tags["covr"]:
        return bytes(tags["covr"][0])
    return None


def folder_art(path: Path) -> Optional[Path]:
    folder = path.parent
    try:
        entries = {entry.name.lower(): entry for entry in folder.iterdir() if entry.is_file()}
    except OSError:
        return None
    for name in FOLDER_ART_NAMES:
        for ext in FOLDER_ART_EXTENSIONS:
            entry = entries.get(name + ext)
            if entry is not None:
                return entry
    return None


def find_art(path: Path) -> Optional[bytes]:
    data = embedded_art(path)
    if data:
        return data
    cover = folder_art(path)
    if cover is not None:
        try:
            return cover.read_bytes()
        except OSError:
            return None
    return None
