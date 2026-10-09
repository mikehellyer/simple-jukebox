"""Runs podcast work in the background and keeps the store up to date.

Network work (fetching feeds and artwork, downloading episodes) happens on
worker threads; every database change happens back on the GUI thread, so
the PodcastStore is only ever touched from one thread. Downloads run one at
a time, in the order they were asked for.
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, Qt, QTimer, Signal

from simple_jukebox.core.http import SSL_CONTEXT
from simple_jukebox.core.podcast_download import (
    DownloadCancelled,
    default_podcasts_folder,
    download_episode,
    episode_relative_path,
    podcast_tags,
    tag_as_podcast,
)
from simple_jukebox.core.podcast_feed import FeedError, fetch_feed
from simple_jukebox.core.podcasts import Episode, PodcastStore

REFRESH_INTERVAL_MS = 2 * 60 * 60 * 1000  # every two hours while running


class _Bridge(QObject):
    """Signals emitted from worker threads, delivered on the GUI thread."""

    feed_fetched = Signal(int, object, str)  # podcast id (0 = new subscription), Feed or None, error / feed url
    download_progress = Signal(int, int, int)  # episode id, bytes done, total
    download_finished = Signal(int, str, str)  # episode id, file path ("" on failure), error
    artwork_ready = Signal(int)  # podcast id


class PodcastManager(QObject):
    changed = Signal()  # shows or episodes changed — refresh what's shown
    download_progress = Signal(int, int, int)
    status = Signal(str)  # a one-line message for the status bar
    subscribed = Signal(int)  # podcast id
    artwork_ready = Signal(int)

    def __init__(self, settings, store: Optional[PodcastStore] = None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.store = store or PodcastStore()
        self._bridge = _Bridge()
        self._bridge.feed_fetched.connect(self._on_feed_fetched, Qt.QueuedConnection)
        self._bridge.download_progress.connect(self._on_download_progress, Qt.QueuedConnection)
        self._bridge.download_finished.connect(self._on_download_finished, Qt.QueuedConnection)
        self._bridge.artwork_ready.connect(self.artwork_ready, Qt.QueuedConnection)

        self._downloads: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._queued: set[int] = set()  # episode ids waiting or downloading
        self._progress: dict[int, tuple[int, int]] = {}
        self._cancelled: set[int] = set()
        self._refreshing = 0
        self._stopped = False
        threading.Thread(target=self._download_worker, name="podcast-downloads", daemon=True).start()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh_all)
        self._timer.start()

    # --- folders --------------------------------------------------------

    def podcasts_folder(self) -> Path:
        return Path(self._settings.podcasts_folder) if self._settings.podcasts_folder else default_podcasts_folder()

    @staticmethod
    def artwork_path(image_url: str) -> Path:
        key = hashlib.sha1(image_url.encode("utf-8")).hexdigest()
        return Path(user_cache_dir("Simple-Jukebox")) / "podcast-art" / f"{key}.img"

    def artwork_bytes(self, podcast_id: int) -> Optional[bytes]:
        podcast = self.store.podcast(podcast_id)
        if podcast is None or not podcast.image_url:
            return None
        try:
            return self.artwork_path(podcast.image_url).read_bytes()
        except OSError:
            return None

    # --- subscribing and refreshing ---------------------------------------

    def subscribe(self, feed_url: str) -> None:
        self.status.emit("Subscribing…")
        self._fetch_in_background(0, feed_url)

    def refresh_all(self) -> None:
        for podcast in self.store.podcasts():
            self._fetch_in_background(podcast.id, podcast.feed_url)

    def refresh(self, podcast_id: int) -> None:
        podcast = self.store.podcast(podcast_id)
        if podcast is not None:
            self._fetch_in_background(podcast.id, podcast.feed_url)

    @property
    def refreshing(self) -> bool:
        return self._refreshing > 0

    def _fetch_in_background(self, podcast_id: int, feed_url: str) -> None:
        self._refreshing += 1
        self.changed.emit()
        bridge = self._bridge

        def work():
            try:
                bridge.feed_fetched.emit(podcast_id, fetch_feed(feed_url), feed_url)
            except FeedError as error:
                bridge.feed_fetched.emit(podcast_id, None, str(error))

        threading.Thread(target=work, name="podcast-feed", daemon=True).start()

    def _on_feed_fetched(self, podcast_id: int, feed, detail: str) -> None:
        self._refreshing = max(0, self._refreshing - 1)
        if feed is None:
            name = self.store.podcast(podcast_id).title if podcast_id and self.store.podcast(podcast_id) else "that feed"
            self.status.emit(f"Couldn't update {name}: {detail}")
            self.changed.emit()
            return
        if podcast_id == 0:
            podcast_id, to_download = self.store.subscribe(detail, feed)
            self.status.emit(f"Subscribed to {feed.title}")
            self.subscribed.emit(podcast_id)
        else:
            to_download = self.store.update_from_feed(podcast_id, feed)
            if to_download:
                count = len(to_download)
                self.status.emit(f"{feed.title}: {count} new episode{'s' if count != 1 else ''}")
        podcast = self.store.podcast(podcast_id)
        if podcast is not None and podcast.auto_download:
            for episode_id in to_download:
                self.download(episode_id)
        if podcast is not None and podcast.image_url:
            self._fetch_artwork(podcast.id, podcast.image_url)
        self.changed.emit()

    def _fetch_artwork(self, podcast_id: int, image_url: str) -> None:
        target = self.artwork_path(image_url)
        if target.exists():
            return
        bridge = self._bridge

        def work():
            request = urllib.request.Request(image_url, headers={"User-Agent": "Simple-Jukebox/1"})
            try:
                with urllib.request.urlopen(request, timeout=15, context=SSL_CONTEXT) as response:
                    data = response.read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except (OSError, ValueError):
                return
            bridge.artwork_ready.emit(podcast_id)

        threading.Thread(target=work, name="podcast-art", daemon=True).start()

    def unsubscribe(self, podcast_id: int) -> None:
        for episode_id in [e.id for e in self.store.episodes(podcast_id) if e.id in self._queued]:
            self.cancel_download(episode_id)
        for path in self.store.unsubscribe(podcast_id):
            self._delete_file(path)
        self.changed.emit()

    def set_options(self, podcast_id: int, auto_download: bool, keep_downloads: int) -> None:
        self.store.set_podcast_options(podcast_id, auto_download, keep_downloads)
        self._apply_keep_limit(podcast_id)
        self.changed.emit()

    # --- downloads --------------------------------------------------------

    def download_state(self, episode_id: int) -> Optional[tuple[int, int]]:
        """None if not downloading; (0, 0) if queued; else (done, total)."""
        if episode_id not in self._queued:
            return None
        return self._progress.get(episode_id, (0, 0))

    def download(self, episode_id: int) -> None:
        episode = self.store.episode(episode_id)
        if episode is None or episode.downloaded or episode_id in self._queued:
            return
        podcast = self.store.podcast(episode.podcast_id)
        target = self.podcasts_folder() / episode_relative_path(podcast.title, episode)
        tags = podcast_tags(podcast.title, podcast.author, podcast.feed_url, episode)
        self._queued.add(episode_id)
        self._cancelled.discard(episode_id)
        self._downloads.put((episode, target, tags))
        self.changed.emit()

    def cancel_download(self, episode_id: int) -> None:
        if episode_id in self._queued:
            self._cancelled.add(episode_id)

    def _download_worker(self) -> None:
        while True:
            job = self._downloads.get()
            if job is None or self._stopped:
                return
            episode, target, tags = job
            if episode.id in self._cancelled:
                self._bridge.download_finished.emit(episode.id, "", "cancelled")
                continue
            try:
                download_episode(
                    episode,
                    target,
                    progress=lambda done, total, i=episode.id: self._bridge.download_progress.emit(i, done, total),
                    cancelled=lambda i=episode.id: i in self._cancelled or self._stopped,
                )
                tag_as_podcast(target, tags)
            except DownloadCancelled:
                self._bridge.download_finished.emit(episode.id, "", "cancelled")
                continue
            except OSError as error:
                self._bridge.download_finished.emit(episode.id, "", str(error))
                continue
            self._bridge.download_finished.emit(episode.id, str(target), "")

    def _on_download_progress(self, episode_id: int, done: int, total: int) -> None:
        self._progress[episode_id] = (done, total)
        self.download_progress.emit(episode_id, done, total)

    def _on_download_finished(self, episode_id: int, path: str, error: str) -> None:
        self._queued.discard(episode_id)
        self._progress.pop(episode_id, None)
        self._cancelled.discard(episode_id)
        episode = self.store.episode(episode_id)
        if path and episode is not None:
            self.store.set_downloaded(episode_id, path)
            self._apply_keep_limit(episode.podcast_id)
        elif path:
            self._delete_file(path)  # unsubscribed while downloading
        elif error and error != "cancelled" and episode is not None:
            self.status.emit(f"Couldn't download “{episode.title}”: {error}")
        self.changed.emit()

    def _apply_keep_limit(self, podcast_id: int) -> None:
        for episode_id in self.store.downloads_beyond_limit(podcast_id):
            self.delete_download(episode_id, notify=False)

    def delete_download(self, episode_id: int, notify: bool = True) -> None:
        path = self.store.clear_download(episode_id)
        if path:
            self._delete_file(path)
        if notify:
            self.changed.emit()

    def _delete_file(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            return
        # Tidy away the show's folder once it's empty.
        folder = Path(path).parent
        if folder != self.podcasts_folder():
            try:
                folder.rmdir()
            except OSError:
                pass

    # --- listening ----------------------------------------------------------

    def set_played(self, episode_ids: list[int], played: bool) -> None:
        for episode_id in episode_ids:
            self.store.set_played(episode_id, played)
        self.changed.emit()

    def save_position(self, episode_id: int, position_ms: int) -> None:
        self.store.set_position(episode_id, position_ms)

    def finished_playing(self, episode_id: int) -> None:
        self.store.set_played(episode_id, True)
        self.changed.emit()

    def episode(self, episode_id: int) -> Optional[Episode]:
        return self.store.episode(episode_id)

    def shutdown(self) -> None:
        self._stopped = True
        self._downloads.put(None)
        self._timer.stop()
