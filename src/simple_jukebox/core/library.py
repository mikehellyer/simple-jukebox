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
"""

# Album order: by disc, then track number, then title for untagged files.
TRACK_ORDER = "album_artist COLLATE NOCASE, year, album COLLATE NOCASE, disc_number, track_number, title COLLATE NOCASE"


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
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


_TRACK_COLUMNS = (
    "id, path, title, artist, album, album_artist, genre, year, track_number, "
    "disc_number, duration, date_added, play_count, last_played, rating"
)


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
            clauses.append("album = ?")
            params.append(album)
        for word in search.split():
            like = f"%{word}%"
            clauses.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ? OR genre LIKE ?)")
            params.extend([like] * 5)
        sql = f"SELECT {_TRACK_COLUMNS} FROM tracks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {TRACK_ORDER}"
        return [Track(**dict(row)) for row in self._conn.execute(sql, params)]

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
