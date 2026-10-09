"""Podcast subscriptions: shows, their episodes, and what's downloaded,
played, and how far into each episode the user has got.

Kept in its own SQLite file beside the music library — podcasts aren't
part of the music library (they never appear in All Music or Albums) and
are synced to a separate Podcasts folder on devices.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from platformdirs import user_data_dir

from simple_jukebox.core.podcast_feed import Feed

SCHEMA = """
CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY,
    feed_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    subscribed_at REAL NOT NULL,
    last_checked REAL,
    auto_download INTEGER NOT NULL DEFAULT 1,
    keep_downloads INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    audio_url TEXT NOT NULL,
    published REAL NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    local_path TEXT,
    downloaded_at REAL,
    played INTEGER NOT NULL DEFAULT 0,
    position_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (podcast_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_episodes_podcast ON episodes(podcast_id, published);
"""

# On subscribing, only this many of the newest episodes are downloaded
# automatically — not a show's whole back catalogue.
DOWNLOAD_ON_SUBSCRIBE = 1


@dataclass
class Podcast:
    id: int
    feed_url: str
    title: str
    author: str
    description: str
    image_url: str
    link: str
    last_checked: Optional[float]
    auto_download: bool
    keep_downloads: int  # newest N downloads kept; 0 = keep them all
    episode_count: int = 0
    downloaded_count: int = 0
    unplayed_count: int = 0


@dataclass
class Episode:
    id: int
    podcast_id: int
    guid: str
    title: str
    audio_url: str
    published: float
    duration: int
    file_size: int
    mime_type: str
    description: str
    local_path: Optional[str]
    played: bool
    position_ms: int
    podcast_title: str = ""

    @property
    def downloaded(self) -> bool:
        return bool(self.local_path) and Path(self.local_path).exists()


def default_db_path() -> Path:
    return Path(user_data_dir("Simple-Jukebox")) / "podcasts.sqlite3"


class PodcastStore:
    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path else default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- shows ----------------------------------------------------------

    def subscribe(self, feed_url: str, feed: Feed) -> tuple[int, list[int]]:
        """Add a show (or refresh it if already subscribed). Returns its id
        and the episodes to download straight away — just the newest."""
        existing = self._conn.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
        if existing:
            return existing["id"], self.update_from_feed(existing["id"], feed)
        cursor = self._conn.execute(
            "INSERT INTO podcasts (feed_url, title, author, description, image_url, link, subscribed_at, last_checked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (feed_url, feed.title, feed.author, feed.description, feed.image_url, feed.link, time.time(), time.time()),
        )
        podcast_id = cursor.lastrowid
        self._insert_episodes(podcast_id, feed)
        self._conn.commit()
        newest = [e.id for e in self.episodes(podcast_id)[:DOWNLOAD_ON_SUBSCRIBE]]
        return podcast_id, newest

    def update_from_feed(self, podcast_id: int, feed: Feed) -> list[int]:
        """Refresh a show from its feed; returns ids of newly found episodes."""
        self._conn.execute(
            "UPDATE podcasts SET title = ?, author = ?, description = ?, image_url = ?, link = ?, last_checked = ? "
            "WHERE id = ?",
            (feed.title, feed.author, feed.description, feed.image_url, feed.link, time.time(), podcast_id),
        )
        new_ids = self._insert_episodes(podcast_id, feed)
        self._conn.commit()
        return new_ids

    def _insert_episodes(self, podcast_id: int, feed: Feed) -> list[int]:
        known = {
            row["guid"]: row["id"]
            for row in self._conn.execute("SELECT guid, id FROM episodes WHERE podcast_id = ?", (podcast_id,))
        }
        new_ids = []
        now = time.time()
        for episode in feed.episodes:
            values = (
                episode.title, episode.audio_url, episode.published, episode.duration,
                episode.file_size, episode.mime_type, episode.description,
            )
            if episode.guid in known:
                # Feeds fix typos and swap in new audio URLs; keep up.
                self._conn.execute(
                    "UPDATE episodes SET title = ?, audio_url = ?, published = ?, duration = ?, file_size = ?, "
                    "mime_type = ?, description = ? WHERE id = ?",
                    values + (known[episode.guid],),
                )
                continue
            cursor = self._conn.execute(
                "INSERT INTO episodes (podcast_id, guid, title, audio_url, published, duration, file_size, "
                "mime_type, description, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (podcast_id, episode.guid) + values + (now,),
            )
            known[episode.guid] = cursor.lastrowid
            new_ids.append(cursor.lastrowid)
        return new_ids

    def unsubscribe(self, podcast_id: int) -> list[str]:
        """Forget a show; returns its downloaded files for the caller to delete."""
        paths = [
            row["local_path"]
            for row in self._conn.execute(
                "SELECT local_path FROM episodes WHERE podcast_id = ? AND local_path IS NOT NULL", (podcast_id,)
            )
        ]
        self._conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))
        self._conn.commit()
        return paths

    def podcasts(self) -> list[Podcast]:
        rows = self._conn.execute(
            "SELECT p.*, COUNT(e.id) AS episode_count, "
            "SUM(CASE WHEN e.local_path IS NOT NULL THEN 1 ELSE 0 END) AS downloaded_count, "
            "SUM(CASE WHEN e.local_path IS NOT NULL AND e.played = 0 THEN 1 ELSE 0 END) AS unplayed_count "
            "FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id "
            "GROUP BY p.id ORDER BY p.title COLLATE NOCASE"
        )
        return [self._podcast(row) for row in rows]

    def podcast(self, podcast_id: int) -> Optional[Podcast]:
        return next((p for p in self.podcasts() if p.id == podcast_id), None)

    @staticmethod
    def _podcast(row) -> Podcast:
        return Podcast(
            id=row["id"], feed_url=row["feed_url"], title=row["title"], author=row["author"],
            description=row["description"], image_url=row["image_url"], link=row["link"],
            last_checked=row["last_checked"], auto_download=bool(row["auto_download"]),
            keep_downloads=row["keep_downloads"], episode_count=row["episode_count"] or 0,
            downloaded_count=row["downloaded_count"] or 0, unplayed_count=row["unplayed_count"] or 0,
        )

    def set_podcast_options(self, podcast_id: int, auto_download: bool, keep_downloads: int) -> None:
        self._conn.execute(
            "UPDATE podcasts SET auto_download = ?, keep_downloads = ? WHERE id = ?",
            (int(auto_download), max(0, int(keep_downloads)), podcast_id),
        )
        self._conn.commit()

    # --- episodes -------------------------------------------------------

    _EPISODE_SELECT = (
        "SELECT e.id, e.podcast_id, e.guid, e.title, e.audio_url, e.published, e.duration, e.file_size, "
        "e.mime_type, e.description, e.local_path, e.played, e.position_ms, p.title AS podcast_title "
        "FROM episodes e JOIN podcasts p ON p.id = e.podcast_id"
    )

    @staticmethod
    def _episode(row) -> Episode:
        data = dict(row)
        data["played"] = bool(data["played"])
        return Episode(**data)

    def episodes(self, podcast_id: int) -> list[Episode]:
        rows = self._conn.execute(
            f"{self._EPISODE_SELECT} WHERE e.podcast_id = ? ORDER BY e.published DESC, e.id DESC", (podcast_id,)
        )
        return [self._episode(row) for row in rows]

    def episode(self, episode_id: int) -> Optional[Episode]:
        row = self._conn.execute(f"{self._EPISODE_SELECT} WHERE e.id = ?", (episode_id,)).fetchone()
        return self._episode(row) if row else None

    def downloaded_episodes(self, podcast_ids: Optional[list[int]] = None, unplayed_only: bool = False) -> list[Episode]:
        """Episodes with a downloaded file, for syncing to a device."""
        sql = f"{self._EPISODE_SELECT} WHERE e.local_path IS NOT NULL"
        params: list = []
        if podcast_ids is not None:
            sql += f" AND e.podcast_id IN ({', '.join('?' * len(podcast_ids)) or 'NULL'})"
            params.extend(podcast_ids)
        if unplayed_only:
            sql += " AND e.played = 0"
        sql += " ORDER BY p.title COLLATE NOCASE, e.published DESC"
        return [self._episode(row) for row in self._conn.execute(sql, params)]

    def set_downloaded(self, episode_id: int, path: str) -> None:
        self._conn.execute(
            "UPDATE episodes SET local_path = ?, downloaded_at = ? WHERE id = ?", (path, time.time(), episode_id)
        )
        self._conn.commit()

    def clear_download(self, episode_id: int) -> Optional[str]:
        """Forget an episode's download; returns the file for the caller to delete."""
        row = self._conn.execute("SELECT local_path FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        self._conn.execute("UPDATE episodes SET local_path = NULL, downloaded_at = NULL WHERE id = ?", (episode_id,))
        self._conn.commit()
        return row["local_path"] if row else None

    def set_played(self, episode_id: int, played: bool) -> None:
        # Marking played/unplayed also resets where to resume from.
        self._conn.execute(
            "UPDATE episodes SET played = ?, position_ms = 0 WHERE id = ?", (int(played), episode_id)
        )
        self._conn.commit()

    def set_position(self, episode_id: int, position_ms: int) -> None:
        self._conn.execute("UPDATE episodes SET position_ms = ? WHERE id = ?", (max(0, int(position_ms)), episode_id))
        self._conn.commit()

    def downloads_beyond_limit(self, podcast_id: int) -> list[int]:
        """Downloaded episodes older than the show's newest keep_downloads
        downloads — the ones to delete to honour that setting."""
        podcast = self.podcast(podcast_id)
        if podcast is None or podcast.keep_downloads <= 0:
            return []
        downloaded = [e.id for e in self.episodes(podcast_id) if e.local_path]
        return downloaded[podcast.keep_downloads:]
