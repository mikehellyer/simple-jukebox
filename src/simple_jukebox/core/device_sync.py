"""Copying music onto a portable player (a Walkman, a phone, an SD card).

A device is just a folder: a USB player mounted over MTP (on Linux, GNOME/
COSMIC expose these under /run/user/<uid>/gvfs/), a player's microSD card
in a card reader, or any USB stick. Songs go in as Artist/Album/NN Title.ext
and playlists as .m3u files beside them, which Android-based Walkmans and
Sailfish phones both pick up.

Sync is one-way and incremental: a song already on the device at the same
size isn't copied again. A manifest file in the destination folder lists
everything this app has put there, so "remove songs that are no longer
selected" only ever deletes files Simple-Jukebox itself copied — never
music the user put on the device some other way.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from simple_jukebox.core.library import Track

MANIFEST_NAME = ".simple-jukebox-sync.json"
COPY_CHUNK_BYTES = 1024 * 1024

# FAT32/exFAT (SD cards) and MTP devices reject these in file names.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME_LENGTH = 120


class SyncCancelled(Exception):
    pass


def safe_name(text: str, fallback: str = "Unknown") -> str:
    """A file/folder name every device filesystem will accept."""
    name = _ILLEGAL_CHARS.sub("_", text).strip()
    # Trailing dots and spaces are silently dropped (or rejected) on FAT.
    name = name.rstrip(". ")
    if len(name) > _MAX_NAME_LENGTH:
        name = name[:_MAX_NAME_LENGTH].rstrip(". ")
    return name or fallback


def device_path_for(track: Track) -> str:
    """Where a track goes on the device, relative to the sync folder,
    always with forward slashes (it's also what .m3u files refer to)."""
    ext = Path(track.path).suffix.lower()
    title = safe_name(track.title, "Untitled")
    if track.track_number:
        disc = f"{track.disc_number}-" if track.disc_number and track.disc_number > 1 else ""
        filename = f"{disc}{track.track_number:02d} {title}{ext}"
    else:
        filename = f"{title}{ext}"
    return "/".join(
        (safe_name(track.album_artist, "Unknown Artist"), safe_name(track.album, "Unknown Album"), filename)
    )


def playlist_file_name(name: str) -> str:
    return safe_name(name, "Playlist") + ".m3u"


def m3u_text(tracks: list[Track]) -> str:
    lines = ["#EXTM3U"]
    for track in tracks:
        lines.append(f"#EXTINF:{int(round(track.duration))},{track.artist} - {track.title}")
        lines.append(device_path_for(track))
    return "\n".join(lines) + "\n"


# --- manifest --------------------------------------------------------------


def read_manifest(dest: Path) -> set[str]:
    try:
        data = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    files = data.get("files") if isinstance(data, dict) else None
    return {f for f in files if isinstance(f, str)} if isinstance(files, list) else set()


def write_manifest(dest: Path, files: Iterable[str]) -> None:
    payload = {"app": "Simple-Jukebox", "files": sorted(set(files))}
    (dest / MANIFEST_NAME).write_text(json.dumps(payload, indent=1), encoding="utf-8")


# --- planning --------------------------------------------------------------


@dataclass
class SyncPlan:
    dest: Path
    copies: list[tuple[str, str]] = field(default_factory=list)  # (source path, device path)
    deletes: list[str] = field(default_factory=list)  # device paths we put there earlier
    playlists: dict[str, str] = field(default_factory=dict)  # .m3u file name → contents
    unchanged: int = 0
    bytes_to_copy: int = 0
    bytes_to_free: int = 0
    wanted: set[str] = field(default_factory=set)  # every device path the sync leaves in place

    @property
    def song_count(self) -> int:
        return len(self.copies) + self.unchanged


def plan_sync(
    dest: Path,
    tracks: Iterable[Track],
    playlists: Optional[dict[str, list[Track]]] = None,
    remove_unselected: bool = True,
) -> SyncPlan:
    """Work out what to copy and delete, without touching the device."""
    dest = Path(dest)
    plan = SyncPlan(dest=dest)
    previously_synced = read_manifest(dest)

    seen: set[str] = set()
    for track in tracks:
        rel = device_path_for(track)
        if rel in seen:
            continue  # the same song selected twice (e.g. via a playlist and its artist)
        seen.add(rel)
        try:
            source_size = os.path.getsize(track.path)
        except OSError:
            continue  # gone from disk since the last library scan
        target = dest / rel
        try:
            on_device = target.stat().st_size
        except OSError:
            on_device = None
        if on_device == source_size:
            plan.unchanged += 1
        elif on_device is not None and rel not in previously_synced:
            # Already there, but not put there by us (copied by hand, or
            # another app) — use it as is rather than overwrite it.
            plan.unchanged += 1
        else:
            plan.copies.append((track.path, rel))
            plan.bytes_to_copy += source_size

    for name, playlist_tracks in (playlists or {}).items():
        # Only list songs that are actually on the device after the sync.
        present = [t for t in playlist_tracks if device_path_for(t) in seen]
        plan.playlists[playlist_file_name(name)] = m3u_text(present)

    plan.wanted = seen | set(plan.playlists)
    if remove_unselected:
        for rel in sorted(previously_synced - plan.wanted):
            plan.deletes.append(rel)
            try:
                plan.bytes_to_free += (dest / rel).stat().st_size
            except OSError:
                pass
    return plan


def free_space(dest: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(dest).free
    except OSError:
        return None  # some MTP mounts can't report it


# --- doing it --------------------------------------------------------------


def _copy_file(source: str, target: Path, cancelled: Callable[[], bool]) -> None:
    """Plain chunked read/write: works on MTP (FUSE) mounts, where
    shutil.copy2's metadata copying and sendfile() fast path can fail."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(source, "rb") as src, open(target, "wb") as out:
            while True:
                if cancelled():
                    raise SyncCancelled()
                chunk = src.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                out.write(chunk)
    except BaseException:
        # Never leave a half-copied song behind — it would look "present"
        # to the player and, at a different size, get recopied anyway.
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _prune_empty_dirs(dest: Path, rel_files: Iterable[str]) -> None:
    for rel in rel_files:
        folder = (dest / rel).parent
        while folder != dest and dest in folder.parents:
            try:
                folder.rmdir()  # only succeeds if empty
            except OSError:
                break
            folder = folder.parent


@dataclass
class SyncResult:
    copied: int = 0
    deleted: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (device path, error)
    cancelled: bool = False


def run_sync(
    plan: SyncPlan,
    progress: Optional[Callable[[int, int, str], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> SyncResult:
    """Carry out a plan. progress(done, total, current device path) is
    called before each step. The manifest is always rewritten at the end
    — even after a cancel or failures — so it matches what's really on
    the device."""
    dest = plan.dest
    result = SyncResult()
    manifest = read_manifest(dest)
    total = len(plan.deletes) + len(plan.copies) + len(plan.playlists)
    done = 0

    def step(label: str) -> None:
        nonlocal done
        if progress is not None:
            progress(done, total, label)
        done += 1

    try:
        # Deletes first, to make room.
        for rel in plan.deletes:
            if cancelled():
                raise SyncCancelled()
            step(rel)
            try:
                (dest / rel).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                result.failed.append((rel, str(error)))
                continue
            manifest.discard(rel)
            result.deleted += 1
        _prune_empty_dirs(dest, plan.deletes)

        for source, rel in plan.copies:
            if cancelled():
                raise SyncCancelled()
            step(rel)
            try:
                _copy_file(source, dest / rel, cancelled)
            except SyncCancelled:
                raise
            except OSError as error:
                result.failed.append((rel, str(error)))
                if getattr(error, "errno", None) == 28:  # ENOSPC: the rest will fail too
                    break
                continue
            manifest.add(rel)
            result.copied += 1

        for name, text in plan.playlists.items():
            if cancelled():
                raise SyncCancelled()
            step(name)
            try:
                # Plain \n and UTF-8 — what Android and Sailfish players expect.
                with open(dest / name, "w", encoding="utf-8", newline="\n") as out:
                    out.write(text)
            except OSError as error:
                result.failed.append((name, str(error)))
                continue
            manifest.add(name)
    except SyncCancelled:
        result.cancelled = True
    finally:
        # Files from earlier syncs that are still wanted stay listed;
        # anything we know is on the device now gets listed.
        try:
            write_manifest(dest, manifest)
        except OSError as error:
            result.failed.append((MANIFEST_NAME, str(error)))
    if progress is not None:
        progress(total, total, "")
    return result


# --- finding devices ---------------------------------------------------------


def candidate_device_folders() -> list[Path]:
    """Folders that look like a plugged-in player or card, to offer in the
    device picker (the user can always browse to any folder instead)."""
    roots: list[Path] = []
    if sys.platform.startswith("linux"):
        gvfs = Path(f"/run/user/{os.getuid()}/gvfs")
        try:
            for mount in sorted(gvfs.iterdir()):
                if mount.name.startswith("mtp:"):
                    # Each MTP device has one folder per storage
                    # ("Internal shared storage", "SD card", …).
                    roots.extend(sorted(p for p in mount.iterdir() if p.is_dir()))
        except OSError:
            pass
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user):
            try:
                roots.extend(sorted(p for p in base.iterdir() if p.is_dir()))
            except OSError:
                pass
    elif sys.platform == "darwin":
        try:
            roots.extend(
                sorted(p for p in Path("/Volumes").iterdir() if p.is_dir() and not p.is_symlink())
            )
        except OSError:
            pass
    elif sys.platform == "win32":
        import ctypes
        import string

        DRIVE_REMOVABLE = 2
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << index):
                root = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOVABLE:
                    roots.append(Path(root))

    # Point straight at the Music folder where there is one.
    found = []
    for root in roots:
        for name in ("Music", "MUSIC", "music"):
            if (root / name).is_dir():
                found.append(root / name)
                break
        else:
            found.append(root)
    return found
