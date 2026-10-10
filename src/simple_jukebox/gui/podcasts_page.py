"""The Podcasts page: subscribed shows on the left; the chosen show's
details, settings and episodes on the right."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.podcasts import Episode, Podcast
from simple_jukebox.core.text import format_duration
from simple_jukebox.gui.podcast_manager import PodcastManager

KEEP_CHOICES = [(0, "All downloads"), (1, "Newest 1"), (3, "Newest 3"), (5, "Newest 5"), (10, "Newest 10")]
ICON_SIZE = 48
EPISODE_ROLE = Qt.UserRole
COL_TITLE, COL_DATE, COL_LENGTH, COL_STATUS = range(4)


def _format_date(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%d %b %Y")


class PodcastsPage(QWidget):
    play_requested = Signal(int)  # episode id
    subscribe_requested = Signal()

    def __init__(self, manager: PodcastManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._filter = ""
        self._rows: dict[int, int] = {}  # episode id → table row
        self._art_cache: dict[tuple, Optional[QPixmap]] = {}
        # Rebuilding the page is the costly part of a podcast refresh, so
        # it only happens while the page is on screen — and a burst of
        # changes (several feeds updating at once) becomes one rebuild.
        self._dirty = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split)

        # Shows
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 4, 0, 0)
        heading = QLabel("Shows")
        heading.setStyleSheet("font-weight: 600; padding-left: 4px;")
        left_layout.addWidget(heading)
        self._shows = QListWidget()
        self._shows.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._shows.setSpacing(2)
        self._shows.currentItemChanged.connect(lambda *_: self._show_selected())
        self._shows.setContextMenuPolicy(Qt.CustomContextMenu)
        self._shows.customContextMenuRequested.connect(self._show_menu)
        left_layout.addWidget(self._shows, stretch=1)
        buttons = QHBoxLayout()
        subscribe = QPushButton("＋ Subscribe…")
        subscribe.clicked.connect(self.subscribe_requested)
        buttons.addWidget(subscribe)
        self._refresh_button = QPushButton("⟳ Refresh All")
        self._refresh_button.clicked.connect(self._manager.refresh_all)
        buttons.addWidget(self._refresh_button)
        left_layout.addLayout(buttons)
        split.addWidget(left)

        # Selected show
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 0, 0)
        header = QHBoxLayout()
        self._art = QLabel()
        self._art.setFixedSize(96, 96)
        self._art.setAlignment(Qt.AlignCenter)
        self._art.setStyleSheet("background-color: rgba(127, 127, 127, 0.15); border-radius: 4px;")
        header.addWidget(self._art)
        info = QVBoxLayout()
        self._title = QLabel("")
        self._title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self._title.setWordWrap(True)
        info.addWidget(self._title)
        self._author = QLabel("")
        self._author.setStyleSheet("color: gray;")
        info.addWidget(self._author)
        self._description = QLabel("")
        self._description.setWordWrap(True)
        self._description.setMaximumHeight(40)
        self._description.setStyleSheet("color: gray; font-size: 11px;")
        info.addWidget(self._description)
        options = QHBoxLayout()
        self._auto_download = QCheckBox("Download new episodes automatically")
        self._auto_download.toggled.connect(self._save_options)
        options.addWidget(self._auto_download)
        options.addWidget(QLabel("Keep:"))
        self._keep = QComboBox()
        for value, label in KEEP_CHOICES:
            self._keep.addItem(label, value)
        self._keep.currentIndexChanged.connect(lambda _i: self._save_options())
        options.addWidget(self._keep)
        options.addStretch(1)
        refresh_one = QPushButton("⟳ Refresh")
        refresh_one.clicked.connect(lambda: self._podcast_id() and self._manager.refresh(self._podcast_id()))
        options.addWidget(refresh_one)
        self._unsubscribe = QPushButton("Unsubscribe")
        self._unsubscribe.clicked.connect(self._unsubscribe_current)
        options.addWidget(self._unsubscribe)
        info.addLayout(options)
        header.addLayout(info, stretch=1)
        right_layout.addLayout(header)

        self._episodes = QTableWidget(0, 4)
        self._episodes.setHorizontalHeaderLabels(["Episode", "Date", "Length", "Status"])
        self._episodes.verticalHeader().hide()
        self._episodes.setShowGrid(False)
        self._episodes.setAlternatingRowColors(True)
        self._episodes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._episodes.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._episodes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._episodes.setWordWrap(False)
        self._episodes.verticalHeader().setDefaultSectionSize(24)
        header_view = self._episodes.horizontalHeader()
        header_view.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        for column, width in ((COL_DATE, 100), (COL_LENGTH, 70), (COL_STATUS, 130)):
            header_view.setSectionResizeMode(column, QHeaderView.Interactive)
            header_view.resizeSection(column, width)
        self._episodes.cellDoubleClicked.connect(lambda row, _col: self._play_row(row))
        self._episodes.setContextMenuPolicy(Qt.CustomContextMenu)
        self._episodes.customContextMenuRequested.connect(self._episode_menu)
        right_layout.addWidget(self._episodes, stretch=1)

        self._empty = QLabel(
            "No podcasts yet.\nClick “＋ Subscribe…” to find a show, or paste its feed address."
        )
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet("color: gray; font-size: 15px;")
        right_layout.addWidget(self._empty)
        split.addWidget(right)
        split.setSizes([260, 900])

        manager.changed.connect(self._schedule_refresh)
        manager.download_progress.connect(self._on_progress)
        manager.subscribed.connect(self._on_subscribed)
        manager.artwork_ready.connect(self._on_artwork_ready)
        self._dirty = True  # built the first time it's shown

    def _schedule_refresh(self) -> None:
        if self.isVisible():
            self._refresh_timer.start()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def _on_subscribed(self, podcast_id: int) -> None:
        self.refresh()
        self._select_podcast(podcast_id)

    def _on_artwork_ready(self, podcast_id: int) -> None:
        self._art_cache = {k: v for k, v in self._art_cache.items() if k[0] != podcast_id}
        self._schedule_refresh()

    # --- shows ----------------------------------------------------------

    def _podcast_id(self) -> Optional[int]:
        item = self._shows.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _select_podcast(self, podcast_id: int) -> None:
        for row in range(self._shows.count()):
            if self._shows.item(row).data(Qt.UserRole) == podcast_id:
                self._shows.setCurrentRow(row)
                return

    def _artwork(self, podcast_id: int, size: int) -> Optional[QPixmap]:
        """Show artwork at the size it's displayed. Feeds often supply
        3000px images; decoding one is slow, so each is decoded once —
        straight to the small size — and remembered."""
        key = (podcast_id, size)
        if key in self._art_cache:
            return self._art_cache[key]
        pixmap = None
        data = self._manager.artwork_bytes(podcast_id)
        if data:
            buffer = QBuffer()
            buffer.setData(QByteArray(data))
            buffer.open(QBuffer.ReadOnly)
            reader = QImageReader(buffer)
            original = reader.size()
            if original.isValid() and original.width() > 0:
                # JPEG decoders can skip most of the work at a reduced size.
                reader.setScaledSize(original.scaled(size * 2, size * 2, Qt.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                pixmap = QPixmap.fromImage(image).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._art_cache[key] = pixmap
        return pixmap

    def refresh(self) -> None:
        self._dirty = False
        self._refresh_timer.stop()
        current = self._podcast_id()
        podcasts = self._manager.store.podcasts()
        self._shows.blockSignals(True)
        self._shows.clear()
        for podcast in podcasts:
            label = podcast.title
            if podcast.unplayed_count:
                label += f"\n{podcast.unplayed_count} unplayed"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, podcast.id)
            item.setToolTip(podcast.title)
            art = self._artwork(podcast.id, ICON_SIZE)
            if art is not None:
                item.setIcon(QIcon(art))
            self._shows.addItem(item)
        self._shows.blockSignals(False)
        if podcasts:
            if current:
                self._select_podcast(current)
            if self._shows.currentItem() is None:
                self._shows.setCurrentRow(0)
        self._refresh_button.setText("⟳ Refreshing…" if self._manager.refreshing else "⟳ Refresh All")
        self._show_selected()

    def _show_selected(self) -> None:
        podcast_id = self._podcast_id()
        podcast = self._manager.store.podcast(podcast_id) if podcast_id else None
        has_show = podcast is not None
        for widget in (self._episodes, self._auto_download, self._keep, self._unsubscribe):
            widget.setVisible(has_show)
        self._empty.setVisible(not has_show)
        if not has_show:
            self._title.setText("")
            self._author.setText("")
            self._description.setText("")
            self._art.clear()
            self._episodes.setRowCount(0)
            return
        self._title.setText(podcast.title)
        self._author.setText(podcast.author)
        self._description.setText(podcast.description)
        self._description.setToolTip(podcast.description)
        art = self._artwork(podcast.id, 96)
        if art is not None:
            self._art.setPixmap(art)
        else:
            self._art.setText("🎙")
        for widget in (self._auto_download, self._keep):
            widget.blockSignals(True)
        self._auto_download.setChecked(podcast.auto_download)
        index = self._keep.findData(podcast.keep_downloads)
        if index < 0:
            self._keep.addItem(f"Newest {podcast.keep_downloads}", podcast.keep_downloads)
            index = self._keep.count() - 1
        self._keep.setCurrentIndex(index)
        for widget in (self._auto_download, self._keep):
            widget.blockSignals(False)
        self._fill_episodes(podcast)

    def _save_options(self, *_args) -> None:
        podcast_id = self._podcast_id()
        if podcast_id:
            self._manager.set_options(podcast_id, self._auto_download.isChecked(), self._keep.currentData() or 0)

    def set_filter(self, text: str) -> None:
        self._filter = text.lower()
        self._show_selected()

    # --- episodes -------------------------------------------------------

    def _fill_episodes(self, podcast: Podcast) -> None:
        selected = {self._episodes.item(i.row(), COL_TITLE).data(EPISODE_ROLE)
                    for i in self._episodes.selectionModel().selectedRows()}
        words = self._filter.split()
        episodes = [
            e for e in self._manager.store.episodes(podcast.id)
            if all(w in f"{e.title} {e.description}".lower() for w in words)
        ]
        self._episodes.setRowCount(len(episodes))
        self._rows = {}
        bold = QFont()
        bold.setBold(True)
        for row, episode in enumerate(episodes):
            self._rows[episode.id] = row
            title = QTableWidgetItem(("● " if not episode.played else "    ") + episode.title)
            title.setData(EPISODE_ROLE, episode.id)
            title.setToolTip(episode.description[:600] if episode.description else episode.title)
            if not episode.played:
                title.setFont(bold)
            self._episodes.setItem(row, COL_TITLE, title)
            self._episodes.setItem(row, COL_DATE, QTableWidgetItem(_format_date(episode.published)))
            length = QTableWidgetItem(format_duration(episode.duration) if episode.duration else "")
            length.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
            self._episodes.setItem(row, COL_LENGTH, length)
            self._episodes.setItem(row, COL_STATUS, QTableWidgetItem(self._status_text(episode)))
            if episode.id in selected:
                self._episodes.selectRow(row)

    def _status_text(self, episode: Episode) -> str:
        state = self._manager.download_state(episode.id)
        if state is not None:
            done, total = state
            if not done:
                return "Queued"
            return f"Downloading {done * 100 // total}%" if total else f"Downloading {done // 1_000_000} MB"
        if episode.downloaded:
            if episode.position_ms and not episode.played:
                return f"⬇ Downloaded · {format_duration(episode.position_ms / 1000)} in"
            return "⬇ Downloaded"
        if episode.position_ms and not episode.played:
            return f"{format_duration(episode.position_ms / 1000)} in"
        return ""

    def _on_progress(self, episode_id: int, done: int, total: int) -> None:
        row = self._rows.get(episode_id)
        if row is not None:
            text = f"Downloading {done * 100 // total}%" if total else f"Downloading {done // 1_000_000} MB"
            self._episodes.item(row, COL_STATUS).setText(text)

    def _selected_episode_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self._episodes.selectionModel().selectedRows()})
        return [self._episodes.item(row, COL_TITLE).data(EPISODE_ROLE) for row in rows]

    def _play_row(self, row: int) -> None:
        item = self._episodes.item(row, COL_TITLE)
        if item is not None:
            self.play_requested.emit(item.data(EPISODE_ROLE))

    def _episode_menu(self, pos) -> None:
        index = self._episodes.indexAt(pos)
        if not index.isValid():
            return
        if not self._episodes.selectionModel().isRowSelected(index.row(), index.parent()):
            self._episodes.selectRow(index.row())
        ids = self._selected_episode_ids()
        episodes = [e for e in (self._manager.episode(i) for i in ids) if e is not None]
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_row(index.row()))
        not_downloaded = [e.id for e in episodes if not e.downloaded and self._manager.download_state(e.id) is None]
        downloading = [e.id for e in episodes if self._manager.download_state(e.id) is not None]
        downloaded = [e.id for e in episodes if e.downloaded]
        if not_downloaded:
            menu.addAction("Download", lambda: [self._manager.download(i) for i in not_downloaded])
        if downloading:
            menu.addAction("Cancel Download", lambda: [self._manager.cancel_download(i) for i in downloading])
        if downloaded:
            menu.addAction("Delete Download", lambda: [self._manager.delete_download(i) for i in downloaded])
        menu.addSeparator()
        if any(not e.played for e in episodes):
            menu.addAction("Mark as Played", lambda: self._manager.set_played(ids, True))
        if any(e.played for e in episodes):
            menu.addAction("Mark as Unplayed", lambda: self._manager.set_played(ids, False))
        if len(episodes) == 1 and episodes[0].downloaded:
            menu.addSeparator()
            folder = str(Path(episodes[0].local_path).parent)
            menu.addAction("Show in Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
        menu.exec(self._episodes.viewport().mapToGlobal(pos))

    def _show_menu(self, pos) -> None:
        item = self._shows.itemAt(pos)
        if item is None:
            return
        podcast_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Refresh", lambda: self._manager.refresh(podcast_id))
        menu.addAction("Mark All as Played", lambda: self._manager.set_played(
            [e.id for e in self._manager.store.episodes(podcast_id)], True))
        menu.addSeparator()
        menu.addAction("Unsubscribe…", lambda: self._unsubscribe(podcast_id))
        menu.exec(self._shows.viewport().mapToGlobal(pos))

    def _unsubscribe_current(self) -> None:
        podcast_id = self._podcast_id()
        if podcast_id:
            self._unsubscribe(podcast_id)

    def _unsubscribe(self, podcast_id: int) -> None:
        podcast = self._manager.store.podcast(podcast_id)
        if podcast is None:
            return
        answer = QMessageBox.question(
            self,
            "Unsubscribe",
            f"Unsubscribe from “{podcast.title}”?\n\n"
            f"Its {podcast.downloaded_count} downloaded episode(s) will be deleted from this computer.",
        )
        if answer == QMessageBox.Yes:
            self._manager.unsubscribe(podcast_id)
