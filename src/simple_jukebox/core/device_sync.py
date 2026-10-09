"""Copying music onto a portable player (a Walkman, a phone, an SD card).

A device is just a folder: a USB player mounted over MTP (on Linux, GNOME/
COSMIC expose these under /run/user/<uid>/gvfs/), a player's microSD card
in a card reader, or any USB stick. Songs go in as Artist/Album/NN Title.ext
and playlists as .m3u files beside them, which Android-based Walkmans and
Sailfish phones both pick up.

Sync is one-way and incremental: a song already on the device at the same
size isn't copied again, and nor is one already there under a different
name (see "recognising songs already on the device" below). A manifest file in the destination folder lists
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


def m3u_text(tracks: list[Track], location: Optional[dict[str, str]] = None) -> str:
    """location maps a track's library path to where it actually is on the
    device, for songs found there under a different name."""
    location = location or {}
    lines = ["#EXTM3U"]
    for track in tracks:
        lines.append(f"#EXTINF:{int(round(track.duration))},{track.artist} - {track.title}")
        lines.append(location.get(track.path, device_path_for(track)))
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


# --- recognising songs already on the device --------------------------------
#
# A song the user put on the player some other way (by hand, another app)
# won't be at the path we'd use, so it's recognised by name instead:
# first by its album folder + file name (free — just a directory listing),
# then, for files whose names don't match anything, by reading their tags
# (slower over USB, so only done when needed).

DURATION_TOLERANCE_SECONDS = 3
_AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav", ".wma", ".aiff", ".aif"}
_LEADING_NUMBER = re.compile(r"^\s*(?:\d{1,2}\s*[-.]\s*)?\d{1,3}\s*(?:[-._)]\s*|\s+)")


def normalize(text: str) -> str:
    """Comparable form of a name: case, accents, punctuation, "&"/"and"
    and a leading "The" don't matter."""
    import unicodedata

    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def title_from_filename(stem: str, artist_hint: str = "") -> str:
    """"01 - Dream Police" / "1-01. Dream Police" / "03 Cheap Trick - Dream
    Police" → "Dream Police"."""
    title = _LEADING_NUMBER.sub("", stem, count=1) or stem
    if " - " in title and artist_hint:
        first, rest = title.split(" - ", 1)
        if normalize(first) == normalize(artist_hint):
            title = rest
    return title


@dataclass
class _DeviceSong:
    rel: str
    tags_read: bool = False
    artist: str = ""
    album: str = ""
    title: str = ""
    duration: float = 0.0


def _device_songs(dest: Path, exclude: set[str]) -> list[_DeviceSong]:
    """Audio files under dest that this app didn't put there."""
    songs = []
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() not in _AUDIO_EXTENSIONS:
                continue
            rel = Path(dirpath, name).relative_to(dest).as_posix()
            if rel not in exclude:
                songs.append(_DeviceSong(rel=rel))
    return songs


def _name_key(album: str, title: str) -> tuple[str, str]:
    return normalize(album), normalize(title)


def load_tag_cache(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_tag_cache(path: Optional[Path], cache: dict) -> None:
    if path is None:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # only a speed-up


class _ExistingSongs:
    """Finds a library track among the songs already on the device.

    Tags read from device files are cached (keyed by path and size) so
    that later syncs don't have to read them over USB again."""

    def __init__(self, dest: Path, songs: list[_DeviceSong], tag_reader, progress=None, cache_path=None):
        self._dest = dest
        self._songs = songs
        self._tag_reader = tag_reader
        self._progress = progress
        self._cache_path = cache_path
        self._by_name: dict[tuple[str, str], str] = {}
        for song in songs:
            parts = song.rel.split("/")
            album = parts[-2] if len(parts) >= 2 else ""
            artist_hint = parts[-3] if len(parts) >= 3 else ""
            title = title_from_filename(Path(parts[-1]).stem, artist_hint)
            self._by_name.setdefault(_name_key(album, title), song.rel)
        self._name_matched: set[str] = set()
        self._by_tags: Optional[dict[tuple[str, str], list[_DeviceSong]]] = None

    def find(self, track: Track) -> Optional[str]:
        rel = self._by_name.get(_name_key(track.album, track.title))
        if rel is not None:
            self._name_matched.add(rel)
            return rel
        return None

    def find_by_tags(self, track: Track) -> Optional[str]:
        if self._by_tags is None:
            self._read_tags()
        for song in self._by_tags.get((normalize(track.artist), normalize(track.title)), []):
            same_album = normalize(song.album) == normalize(track.album)
            close_length = (
                song.duration > 0 and track.duration > 0
                and abs(song.duration - track.duration) <= DURATION_TOLERANCE_SECONDS
            )
            if same_album or close_length:
                return song.rel
        return None

    def _read_tags(self) -> None:
        self._by_tags = {}
        old_cache = load_tag_cache(self._cache_path)
        new_cache: dict = {}
        pending = [s for s in self._songs if s.rel not in self._name_matched]
        for index, song in enumerate(pending):
            try:
                size = (self._dest / song.rel).stat().st_size
            except OSError:
                continue
            cached = old_cache.get(song.rel)
            if isinstance(cached, list) and len(cached) == 6 and cached[0] == size:
                entry = cached
            else:
                if self._progress is not None:
                    self._progress(index, len(pending), song.rel)
                info = self._tag_reader(self._dest / song.rel)
                entry = (
                    [size, info.artist, info.album_artist, info.album, info.title, info.duration]
                    if info is not None else [size, "", "", "", "", 0.0]
                )
            new_cache[song.rel] = entry
            _, artist, album_artist, album, title, duration = entry
            if not title:
                continue
            song.tags_read = True
            song.artist, song.album, song.title, song.duration = artist, album, title, float(duration)
            for name in {artist, album_artist}:
                self._by_tags.setdefault((normalize(name), normalize(title)), []).append(song)
        save_tag_cache(self._cache_path, new_cache)


# --- planning --------------------------------------------------------------


@dataclass
class SyncPlan:
    dest: Path
    copies: list[tuple[str, str]] = field(default_factory=list)  # (source path, device path)
    deletes: list[str] = field(default_factory=list)  # device paths we put there earlier, now unselected
    duplicate_deletes: list[str] = field(default_factory=list)  # our earlier copies of songs the user already had
    playlists: dict[str, str] = field(default_factory=dict)  # .m3u file name → contents
    unchanged: int = 0
    found_existing: int = 0  # selected songs already on the device under another name
    bytes_to_copy: int = 0
    bytes_to_free: int = 0
    wanted: set[str] = field(default_factory=set)  # every device path the sync leaves in place

    @property
    def song_count(self) -> int:
        return len(self.copies) + self.unchanged + self.found_existing

    @property
    def all_deletes(self) -> list[str]:
        return self.duplicate_deletes + self.deletes


def plan_sync(
    dest: Path,
    tracks: Iterable[Track],
    playlists: Optional[dict[str, list[Track]]] = None,
    remove_unselected: bool = True,
    find_existing: bool = True,
    tag_reader: Optional[Callable] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    tag_cache_path: Optional[Path] = None,
) -> SyncPlan:
    """Work out what to copy and delete, without changing the device.

    With find_existing, a selected song that's already on the device under
    a different name (copied by hand or by another app) isn't copied again
    — and if an earlier sync already made a second copy of it, that copy
    is removed. progress(done, total, path) reports tag reading, the slow
    part of that check.
    """
    dest = Path(dest)
    plan = SyncPlan(dest=dest)
    previously_synced = read_manifest(dest)
    existing = None
    if find_existing:
        if tag_reader is None:
            from simple_jukebox.core.tags import read_track as tag_reader
        existing = _ExistingSongs(
            dest, _device_songs(dest, previously_synced), tag_reader, progress, tag_cache_path
        )

    seen: set[str] = set()  # our device paths of selected songs
    location: dict[str, str] = {}  # library path → where the song is on the device
    needs_tag_check: list[tuple[Track, str, int, Optional[int]]] = []

    def keep(track: Track, rel: str) -> None:
        plan.unchanged += 1
        location[track.path] = rel
        plan.wanted.add(rel)

    def ours(track: Track, rel: str, size: int, on_device: Optional[int]) -> None:
        """Our own copy at our path: keep it if current, else (re)copy."""
        if on_device == size:
            keep(track, rel)
        else:
            plan.copies.append((track.path, rel))
            plan.bytes_to_copy += size
            location[track.path] = rel
            plan.wanted.add(rel)

    def use_existing(track: Track, rel: str, our_rel: str) -> None:
        plan.found_existing += 1
        location[track.path] = rel
        plan.wanted.add(rel)
        if our_rel in previously_synced and (dest / our_rel).exists():
            # An earlier sync made a second copy of a song the user had.
            plan.duplicate_deletes.append(our_rel)

    for track in tracks:
        rel = device_path_for(track)
        if rel in seen:
            continue  # the same song selected twice (e.g. via a playlist and its artist)
        seen.add(rel)
        try:
            source_size = os.path.getsize(track.path)
        except OSError:
            continue  # gone from disk since the last library scan

        found = existing.find(track) if existing is not None else None
        if found is not None:
            use_existing(track, found, rel)
            continue
        try:
            on_device = (dest / rel).stat().st_size
        except OSError:
            on_device = None
        if on_device is not None and rel not in previously_synced:
            keep(track, rel)  # the user's own file at exactly our path — never overwritten
        elif existing is not None:
            needs_tag_check.append((track, rel, source_size, on_device))
        else:
            ours(track, rel, source_size, on_device)

    # Only read tags (slow over USB, and cached) for songs no file name matched.
    for track, rel, source_size, on_device in needs_tag_check:
        found = existing.find_by_tags(track)
        if found is not None:
            use_existing(track, found, rel)
        else:
            ours(track, rel, source_size, on_device)

    for name, playlist_tracks in (playlists or {}).items():
        # Only list songs that are actually on the device after the sync.
        present = [t for t in playlist_tracks if t.path in location]
        plan.playlists[playlist_file_name(name)] = m3u_text(present, location)

    plan.wanted |= set(plan.playlists)
    if remove_unselected:
        plan.deletes = sorted(previously_synced - plan.wanted - set(plan.duplicate_deletes))
    for rel in plan.all_deletes:
        try:
            plan.bytes_to_free += (dest / rel).stat().st_size
        except OSError:
            pass
    return plan


def plan_file_sync(dest: Path, items: Iterable[tuple[str, str]], remove_unselected: bool = True) -> SyncPlan:
    """Plan copying plain (source path, device path) files — used for
    podcast episodes, which live in their own folder on the device with
    their own manifest. Same rules as songs: unchanged files aren't
    recopied, the user's own files are never overwritten, and only files
    this app copied are ever removed."""
    dest = Path(dest)
    plan = SyncPlan(dest=dest)
    previously_synced = read_manifest(dest)
    for source, rel in items:
        if rel in plan.wanted:
            continue
        try:
            size = os.path.getsize(source)
        except OSError:
            continue
        try:
            on_device = (dest / rel).stat().st_size
        except OSError:
            on_device = None
        plan.wanted.add(rel)
        if on_device == size or (on_device is not None and rel not in previously_synced):
            plan.unchanged += 1
        else:
            plan.copies.append((source, rel))
            plan.bytes_to_copy += size
    if remove_unselected:
        plan.deletes = sorted(previously_synced - plan.wanted)
        for rel in plan.deletes:
            try:
                plan.bytes_to_free += (dest / rel).stat().st_size
            except OSError:
                pass
    return plan


def default_podcast_folder_for(music_folder: str) -> str:
    """The Podcasts folder beside a device's Music folder — on Android
    (e.g. a Walkman) files under Podcasts/ are treated as podcasts."""
    if not music_folder:
        return ""
    music = Path(music_folder)
    for name in ("Podcasts", "PODCASTS", "podcasts"):
        if (music.parent / name).is_dir():
            return str(music.parent / name)
    return str(music.parent / "Podcasts")


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
    deletes = plan.all_deletes
    total = len(deletes) + len(plan.copies) + len(plan.playlists)
    done = 0

    def step(label: str) -> None:
        nonlocal done
        if progress is not None:
            progress(done, total, label)
        done += 1

    try:
        # Deletes first, to make room.
        for rel in deletes:
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
        _prune_empty_dirs(dest, deletes)

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
