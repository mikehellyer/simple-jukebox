"""Knowing which player is plugged in, and where its podcasts belong.

Two Walkmans can mount at exactly the same place (both show up as a
"WALKMAN" drive), so a device's folder alone doesn't say which device it
is. On its first sync each device gets a small identity file — a random
id beside the sync manifest in its Music folder — and every later sync
checks it, refusing to sync one device's playlists onto another.

Players also disagree about podcasts: Android ones (e.g. the NW-A306)
list anything in the Podcasts folder at the top of their storage as a
podcast, while Sony's own-firmware players (e.g. the NW-A55) only read
MUSIC and LEARNING, so episodes have to go inside the Music folder.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

IDENTITY_NAME = ".simple-jukebox-device.json"

PODCASTS_BESIDE_MUSIC = "beside"  # <storage>/Podcasts — Android players
PODCASTS_IN_MUSIC = "inside"  # <Music>/Podcasts — players that only read MUSIC
PODCASTS_CUSTOM = "custom"
PODCAST_LOCATIONS = (PODCASTS_BESIDE_MUSIC, PODCASTS_IN_MUSIC, PODCASTS_CUSTOM)
PODCASTS_SUBFOLDER = "Podcasts"


def new_device_id() -> str:
    return uuid.uuid4().hex


def read_identity(folder: Path) -> Optional[dict]:
    """The identity in a device folder, or None if it has none (or the
    folder isn't there — e.g. the device isn't plugged in)."""
    try:
        data = json.loads((Path(folder) / IDENTITY_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        return None
    return data


def write_identity(folder: Path, device_id: str, name: str) -> None:
    payload = {"app": "Simple-Jukebox", "id": device_id, "name": name, "created": int(time.time())}
    (Path(folder) / IDENTITY_NAME).write_text(json.dumps(payload, indent=1), encoding="utf-8")


class DeviceCheck(Enum):
    MATCH = "match"  # this folder is the expected device
    NEW = "new"  # never synced (no identity yet) — safe to claim for a first sync
    UNMARKED = "unmarked"  # expected a known device, but this folder has no identity
    OTHER = "other"  # a different device's folder
    NOT_FOUND = "not_found"  # the folder isn't there (not plugged in?)


@dataclass
class CheckResult:
    status: DeviceCheck
    found_id: str = ""
    found_name: str = ""  # the name written into the folder's identity, if any


def check_device(folder: Path, expected_id: str) -> CheckResult:
    """Is the device at folder the one with expected_id ("" = a device
    profile that has never synced yet)?"""
    folder = Path(folder)
    if not folder.is_dir():
        return CheckResult(DeviceCheck.NOT_FOUND)
    identity = read_identity(folder)
    if identity is None:
        return CheckResult(DeviceCheck.UNMARKED if expected_id else DeviceCheck.NEW)
    found_id, found_name = identity["id"], str(identity.get("name", ""))
    if expected_id and found_id == expected_id:
        return CheckResult(DeviceCheck.MATCH, found_id, found_name)
    return CheckResult(DeviceCheck.OTHER, found_id, found_name)


# --- where podcasts go ----------------------------------------------------------


def looks_like_android(music_folder: str) -> bool:
    """Android storage has an "Android" folder at its top, beside Music."""
    if not music_folder:
        return False
    return (Path(music_folder).parent / "Android").is_dir()


def default_podcast_location(music_folder: str) -> str:
    return PODCASTS_BESIDE_MUSIC if looks_like_android(music_folder) else PODCASTS_IN_MUSIC


def podcast_folder(music_folder: str, location: str, custom: str = "") -> str:
    """The folder episodes sync into, for a device's chosen location."""
    if location == PODCASTS_CUSTOM:
        return custom
    if not music_folder:
        return ""
    music = Path(music_folder)
    if location == PODCASTS_IN_MUSIC:
        return str(music / PODCASTS_SUBFOLDER)
    # Beside Music: use an existing Podcasts folder's real name, whatever
    # its case (FAT and macOS ignore case, but the name shown should be real).
    try:
        for entry in sorted(music.parent.iterdir()):
            if entry.name.lower() == "podcasts" and entry.is_dir():
                return str(entry)
    except OSError:
        pass
    return str(music.parent / PODCASTS_SUBFOLDER)
