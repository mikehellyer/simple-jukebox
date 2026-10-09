"""The music library: a SQLite database of every track found under the
user's music folders, plus play counts and ratings.

Scanning is incremental — a file whose size and modified-time haven't
changed since the last scan isn't re-read — so rescanning a large
library is quick. Files that have disappeared are dropped.

A Library is used from one thread at a time. The GUI scans on a
background thread with its own Library instance on the same database
file, then refreshes its views from the main thread's instance.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from platformdirs import user_data_dir

from simple_jukebox.core.tags import TrackInfo, is_audio_file, read_track

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    album_artist TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '',
    year INTEGER,
    track_number INTEGER,
    disc_number INTEGER,
    duration REAL NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime REAL NOT NULL DEFAULT 0,
    date_added REAL NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played REAL,
    rating INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_album_artist ON tracks(album_artist);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL
);
-- A track can appear in a playlist more than once, so entries are keyed
-- by position rather than (playlist, track). Deleting a track from the
-- library (its file is gone) drops it from every playlist.
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (playlist_id, position)
);
"""

# Album order: by disc, then track number, then title for untagged files.
TRACK_ORDER = "album_artist COLLATE NOCASE, year, album COLLATE NOCASE, disc_number, track_number, title COLLATE NOCASE"


# "Live [Disc 1]", "Live (CD 2)", "Live - Disc One", "Live, Disk 2 of 2" …
# — one album split across discs in its tags.
_DISC_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_DISC_SUFFIX = re.compile(
    # \b: "CD" must be a word of its own — not the end of a catalogue
    # number like "[MFSL UDCD-788]"; and real disc numbers are 1–2 digits.
    r"\s*(?:[-–—,:]\s*)?[\[\(\{]?\s*\b(?:disc|disk|cd)\s*[-.#]?\s*"
    r"(\d{1,2}|" + "|".join(_DISC_WORDS) + r")\b"
    r"(?:\s*(?:of|/)\s*\d+)?\s*[\]\)\}]?\s*$",
    re.IGNORECASE,
)


def split_disc(album: str) -> tuple[str, Optional[int]]:
    """("Live [Disc 2]") → ("Live", 2); albums without a disc marker are
    returned unchanged, with None."""
    match = _DISC_SUFFIX.search(album or "")
    if not match:
        return album, None
    base = album[: match.start()].rstrip(" -–—,:")
    if not base:
        return album, None  # the whole name is "CD 1" — leave it alone
    number = match.group(1).lower()
    return base, _DISC_WORDS.get(number) or int(number)


def album_base(album: str) -> str:
    return split_disc(album)[0]


def _album_order(track: "Track") -> tuple:
    """Order within a (possibly multi-disc) album: disc, then track."""
    disc = track.disc_number or split_disc(track.album)[1] or 0
    return (disc, track.track_number or 0, track.title.lower())


@dataclass
class Track:
    id: int
    path: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    year: Optional[int]
    track_number: Optional[int]
    disc_number: Optional[int]
    duration: float
    date_added: float
    play_count: int
    last_played: Optional[float]
    rating: int


@dataclass
class Playlist:
    id: int
    name: str
    track_count: int


@dataclass
class AlbumSummary:
    """One album as the grid shows it."""

    album_artist: str
    album: str
    year: Optional[int]
    track_count: int
    cover_path: str  # its first track — where to look for cover art

    @property
    def key(self) -> tuple[str, str]:
        return self.album_artist, self.album


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


_TRACK_COLUMNS = (
    "id, path, title, artist, album, album_artist, genre, year, track_number, "
    "disc_number, duration, date_added, play_count, last_played, rating"
)


def already_in_playlist(existing_ids: Iterable[int], new_ids: list[int]) -> list[int]:
    """Indexes into new_ids of songs that would end up in the playlist
    twice — already in it, or repeated earlier in new_ids itself."""
    present = set(existing_ids)
    duplicates = []
    for index, track_id in enumerate(new_ids):
        if track_id in present:
            duplicates.append(index)
        present.add(track_id)
    return duplicates


def default_db_path() -> Path:
    return Path(user_data_dir("Simple-Jukebox")) / "library.sqlite3"


def iter_audio_files(folders: Iterable[str]) -> Iterable[Path]:
    for folder in folders:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if is_audio_file(path):
                    yield path


class Library:
    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path else default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        # Off by default in SQLite, and per-connection — needed for the
        # playlist ON DELETE CASCADE rules.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._conn.close()

    # --- scanning -------------------------------------------------------

    def scan(
        self,
        folders: Iterable[str],
        progress: Optional[Callable[[int, str], None]] = None,
        read: Callable[[Path], Optional[TrackInfo]] = read_track,
    ) -> ScanResult:
        """Bring the database in line with what's on disk under folders.

        progress(files_seen, current_path) is called as files are visited,
        for a "Scanning… 1,204 files" style indicator.
        """
        result = ScanResult()
        known = {
            row["path"]: (row["file_size"], row["file_mtime"])
            for row in self._conn.execute("SELECT path, file_size, file_mtime FROM tracks")
        }
        seen: set[str] = set()
        now = time.time()

        for count, path in enumerate(iter_audio_files(folders), start=1):
            key = str(path)
            seen.add(key)
            if progress is not None:
                progress(count, key)
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint = (stat.st_size, stat.st_mtime)
            if known.get(key) == fingerprint:
                result.unchanged += 1
                continue
            info = read(path)
            if info is None:
                continue
            if key in known:
                self._update_track(info, fingerprint)
                result.updated += 1
            else:
                self._insert_track(info, fingerprint, now)
                result.added += 1

        missing = [path for path in known if path not in seen]
        for path in missing:
            self._conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
        result.removed = len(missing)
        self._conn.commit()
        return result

    def _insert_track(self, info: TrackInfo, fingerprint: tuple[int, float], now: float) -> None:
        self._conn.execute(
            "INSERT INTO tracks (path, title, artist, album, album_artist, genre, year, "
            "track_number, disc_number, duration, file_size, file_mtime, date_added) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                info.path, info.title, info.artist, info.album, info.album_artist, info.genre,
                info.year, info.track_number, info.disc_number, info.duration,
                fingerprint[0], fingerprint[1], now,
            ),
        )

    def _update_track(self, info: TrackInfo, fingerprint: tuple[int, float]) -> None:
        # Play count, rating and date added survive a retag.
        self._conn.execute(
            "UPDATE tracks SET title = ?, artist = ?, album = ?, album_artist = ?, genre = ?, "
            "year = ?, track_number = ?, disc_number = ?, duration = ?, file_size = ?, "
            "file_mtime = ? WHERE path = ?",
            (
                info.title, info.artist, info.album, info.album_artist, info.genre, info.year,
                info.track_number, info.disc_number, info.duration,
                fingerprint[0], fingerprint[1], info.path,
            ),
        )

    def remove_folder(self, folder: str) -> int:
        """Forget every track under folder (when it's removed from the library)."""
        prefix = str(Path(folder)).rstrip(os.sep) + os.sep
        cursor = self._conn.execute(
            "DELETE FROM tracks WHERE substr(path, 1, ?) = ?", (len(prefix), prefix)
        )
        self._conn.commit()
        return cursor.rowcount

    # --- browsing -------------------------------------------------------

    def track_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def artists(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT album_artist FROM tracks ORDER BY album_artist COLLATE NOCASE"
        )
        return [row[0] for row in rows]

    def albums(self, artist: Optional[str] = None) -> list[tuple[str, str, Optional[int]]]:
        """(album_artist, album, year) for every album, optionally for one
        artist. Discs of one album ("… [Disc 1]", "… [Disc 2]") count as one."""
        merged: dict[tuple[str, str], Optional[int]] = {}
        for album_artist, name, year in self._album_rows(artist):
            key = (album_artist, album_base(name))
            if key not in merged:
                merged[key] = year
            elif year and (merged[key] is None or year > merged[key]):
                merged[key] = year  # latest year of any disc, like MAX(year)
        return [(artist_name, name, year) for (artist_name, name), year in merged.items()]

    def _album_rows(self, artist: Optional[str] = None) -> list[tuple[str, str, Optional[int]]]:
        """(album_artist, album, year) for every album, optionally for one artist."""
        sql = "SELECT album_artist, album, MAX(year) FROM tracks"
        params: list = []
        if artist is not None:
            sql += " WHERE album_artist = ?"
            params.append(artist)
        sql += " GROUP BY album_artist, album ORDER BY album_artist COLLATE NOCASE, MAX(year), album COLLATE NOCASE"
        return [(row[0], row[1], row[2]) for row in self._conn.execute(sql, params)]

    def tracks(
        self,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        search: str = "",
    ) -> list[Track]:
        """Tracks filtered by artist/album and a free-text search across
        title, artist, album and genre. Every search word must match
        somewhere (so "beatles abbey" finds Abbey Road)."""
        clauses: list[str] = []
        params: list = []
        if artist is not None:
            clauses.append("album_artist = ?")
            params.append(artist)
        if album is not None:
            # Matches every disc of a multi-disc album ("Live" finds
            # "Live [Disc 1]" and "Live [Disc 2]"); refined in Python below.
            base = album_base(album)
            clauses.append("(album = ? OR album LIKE ? ESCAPE '\\')")
            params.extend([base, base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"])
        for word in search.split():
            like = f"%{word}%"
            clauses.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ? OR genre LIKE ?)")
            params.extend([like] * 5)
        sql = f"SELECT {_TRACK_COLUMNS} FROM tracks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {TRACK_ORDER}"
        tracks = [Track(**dict(row)) for row in self._conn.execute(sql, params)]
        if album is not None:
            base = album_base(album)
            tracks = [t for t in tracks if album_base(t.album) == base]
            tracks.sort(key=lambda t: (t.album_artist.lower(), _album_order(t)))
        return tracks

    def album_summaries(self, search: str = "") -> list[AlbumSummary]:
        """Every album (optionally only those with a song matching search),
        in artist, year, album order."""
        summaries: dict[tuple[str, str], AlbumSummary] = {}
        for track in self.tracks(search=search):
            base = album_base(track.album)
            key = (track.album_artist, base)
            summary = summaries.get(key)
            if summary is None:
                summaries[key] = AlbumSummary(
                    album_artist=track.album_artist,
                    album=base,
                    year=track.year,
                    track_count=1,
                    cover_path=track.path,
                )
            else:
                summary.track_count += 1
                if track.year and (summary.year is None or track.year > summary.year):
                    summary.year = track.year
        return list(summaries.values())

    def album_tracks(self, album_artist: str, album: str) -> list[Track]:
        return self.tracks(artist=album_artist, album=album)

    def recently_added(self, limit: int = 200) -> list[Track]:
        rows = self._conn.execute(
            f"SELECT {_TRACK_COLUMNS} FROM tracks ORDER BY date_added DESC, {TRACK_ORDER} LIMIT ?", (limit,)
        )
        return [Track(**dict(row)) for row in rows]

    def most_played(self, limit: int = 100) -> list[Track]:
        rows = self._conn.execute(
            f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE play_count > 0 "
            "ORDER BY play_count DESC, last_played DESC LIMIT ?",
            (limit,),
        )
        return [Track(**dict(row)) for row in rows]

    def track(self, track_id: int) -> Optional[Track]:
        row = self._conn.execute(f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return Track(**dict(row)) if row else None

    def tracks_by_ids(self, track_ids: Iterable[int]) -> dict[int, Track]:
        """Look up many tracks at once (e.g. a long Up Next list)."""
        unique = list(dict.fromkeys(track_ids))
        found: dict[int, Track] = {}
        # SQLite caps the number of ? parameters per statement.
        for start in range(0, len(unique), 500):
            chunk = unique[start:start + 500]
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE id IN ({placeholders})", chunk)
            for row in rows:
                found[row["id"]] = Track(**dict(row))
        return found

    # --- stats ----------------------------------------------------------

    def record_play(self, track_id: int, when: Optional[float] = None) -> None:
        self._conn.execute(
            "UPDATE tracks SET play_count = play_count + 1, last_played = ? WHERE id = ?",
            (when if when is not None else time.time(), track_id),
        )
        self._conn.commit()

    def set_rating(self, track_id: int, rating: int) -> None:
        self._conn.execute(
            "UPDATE tracks SET rating = ? WHERE id = ?", (max(0, min(5, int(rating))), track_id)
        )
        self._conn.commit()

    # --- playlists ------------------------------------------------------

    def playlists(self) -> list[Playlist]:
        rows = self._conn.execute(
            "SELECT p.id, p.name, COUNT(pt.track_id) FROM playlists p "
            "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "GROUP BY p.id ORDER BY p.name COLLATE NOCASE, p.id"
        )
        return [Playlist(id=row[0], name=row[1], track_count=row[2]) for row in rows]

    def create_playlist(self, name: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO playlists (name, created) VALUES (?, ?)", (name.strip() or "Untitled Playlist", time.time())
        )
        self._conn.commit()
        return cursor.lastrowid

    def rename_playlist(self, playlist_id: int, name: str) -> None:
        if name.strip():
            self._conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name.strip(), playlist_id))
            self._conn.commit()

    def delete_playlist(self, playlist_id: int) -> None:
        self._conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        self._conn.commit()

    def playlist_tracks(self, playlist_id: int) -> list[Track]:
        columns = ", ".join(f"t.{c.strip()}" for c in _TRACK_COLUMNS.split(","))
        rows = self._conn.execute(
            f"SELECT {columns} FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
            "WHERE pt.playlist_id = ? ORDER BY pt.position",
            (playlist_id,),
        )
        return [Track(**dict(row)) for row in rows]

    def playlist_track_ids(self, playlist_id: int) -> list[int]:
        """Track ids in playlist order (repeats included)."""
        rows = self._conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,)
        )
        return [row[0] for row in rows]

    def set_playlist_tracks(self, playlist_id: int, track_ids: list[int]) -> None:
        """Replace a playlist's contents (positions are renumbered 0..n-1)."""
        self._conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        self._conn.executemany(
            "INSERT INTO playlist_tracks (playlist_id, position, track_id) VALUES (?, ?, ?)",
            [(playlist_id, position, track_id) for position, track_id in enumerate(track_ids)],
        )
        self._conn.commit()

    def add_to_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        self.set_playlist_tracks(playlist_id, self.playlist_track_ids(playlist_id) + list(track_ids))

    def remove_from_playlist(self, playlist_id: int, positions: Iterable[int]) -> None:
        """Remove entries by their position in the playlist (0-based)."""
        drop = set(positions)
        kept = [tid for pos, tid in enumerate(self.playlist_track_ids(playlist_id)) if pos not in drop]
        self.set_playlist_tracks(playlist_id, kept)

    def move_in_playlist(self, playlist_id: int, positions: list[int], to_position: int) -> None:
        """Move the entries at positions (kept in their relative order) so
        the first lands at to_position — counted in the playlist as it was
        before the move, like a drag-and-drop insertion point."""
        track_ids = self.playlist_track_ids(playlist_id)
        moving_positions = sorted(p for p in set(positions) if 0 <= p < len(track_ids))
        moving = [track_ids[p] for p in moving_positions]
        insert_at = to_position - sum(1 for p in moving_positions if p < to_position)
        rest = [tid for pos, tid in enumerate(track_ids) if pos not in set(moving_positions)]
        insert_at = max(0, min(insert_at, len(rest)))
        self.set_playlist_tracks(playlist_id, rest[:insert_at] + moving + rest[insert_at:])
