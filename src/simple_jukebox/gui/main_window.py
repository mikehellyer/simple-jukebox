from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox import __version__
from simple_jukebox.core.artwork import find_art
from simple_jukebox.core.library import Library, Track
from simple_jukebox.core.play_queue import PlayQueue
from simple_jukebox.core.self_update import (
    download_asset,
    find_asset_for_this_platform,
    launch_installer,
)
from simple_jukebox.core.settings import SettingsStore
from simple_jukebox.core.updater import API_TIMEOUT_SECONDS, check_for_update
from simple_jukebox.gui.player_bar import PlayerBar
from simple_jukebox.gui.track_model import COLUMNS, RATING_COLUMN, TrackTableModel
from simple_jukebox.gui.update_banner import UpdateBanner

UPDATE_OWNER = "mikehellyer"
UPDATE_REPO = "simple-jukebox"
BACKGROUND_JOIN_TIMEOUT_MS = (API_TIMEOUT_SECONDS + 1) * 1000
ICON_PATH = Path(__file__).parent / "resources" / "icon.png"

SOURCE_ALL = "All Music"
SOURCE_RECENT = "Recently Added"
SOURCE_MOST_PLAYED = "Most Played"
ALL_ARTISTS = "All Artists"
ALL_ALBUMS = "All Albums"

# A track counts as "played" once this much of it has been heard, the
# same rule of thumb iTunes and Last.fm use — skipping after a few
# seconds shouldn't bump its play count.
PLAYED_FRACTION = 0.5


class _CallableWorker(QObject):
    """Runs a zero-arg callable on a background thread and emits its result."""

    finished = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        self.finished.emit(self._fn())


class _ScanProgressBridge(QObject):
    """Lets the scanning thread report progress to the GUI thread safely."""

    progress = Signal(int)


class _NaturalSortProxy(QSortFilterProxyModel):
    """Sorts the numeric columns as numbers rather than text ("10" after "9")."""

    NUMERIC_COLUMNS = {1, 5, 7, 8, RATING_COLUMN}

    def lessThan(self, left, right) -> bool:
        if left.column() in self.NUMERIC_COLUMNS:
            model = self.sourceModel()
            a, b = model.track_at(left.row()), model.track_at(right.row())
            key = {
                1: lambda t: t.track_number or 0,
                5: lambda t: t.year or 0,
                7: lambda t: t.duration,
                8: lambda t: t.play_count,
                RATING_COLUMN: lambda t: t.rating,
            }[left.column()]
            return key(a) < key(b)
        return super().lessThan(left, right)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Simple-Jukebox v{__version__}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1280, 780)

        self._settings = SettingsStore()
        self._library = Library()
        self._queue = PlayQueue()
        self._queue.shuffle = self._settings.shuffle
        self._queue.repeat = self._settings.repeat
        self._current: Optional[Track] = None
        self._play_recorded = False
        self._scanning = False
        self._background_threads: list[QThread] = []
        self._background_workers: list[_CallableWorker] = []
        self._pending_update = None

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_player_error)

        self._build_ui()
        self._build_menus()

        self._player_bar.set_volume(self._settings.volume)
        self._audio_output.setVolume(self._settings.volume / 100)
        self._player_bar.set_shuffle(self._settings.shuffle)
        self._player_bar.set_repeat(self._settings.repeat)

        self._refresh_browser()
        if self._settings.music_folders:
            self._start_scan()
        self._check_for_updates()

    # --- layout ---------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._update_banner = UpdateBanner()
        self._update_banner.update_clicked.connect(self._start_update)
        layout.addWidget(self._update_banner)

        self._player_bar = PlayerBar()
        self._player_bar.play_pause_requested.connect(self._toggle_play_pause)
        self._player_bar.previous_requested.connect(self._play_previous)
        self._player_bar.next_requested.connect(lambda: self._play_next(user_requested=True))
        self._player_bar.seek_requested.connect(self._player.setPosition)
        self._player_bar.volume_changed.connect(self._on_volume_changed)
        self._player_bar.shuffle_toggled.connect(self._on_shuffle_toggled)
        self._player_bar.repeat_changed.connect(self._on_repeat_changed)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 12, 0)
        top_row.addWidget(self._player_bar, stretch=1)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search library…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(240)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._refresh_tracks)
        self._search.textChanged.connect(lambda _text: self._search_timer.start())
        top_row.addWidget(self._search)
        layout.addLayout(top_row)

        # Left: library sources. Right: artist/album column browser above
        # the track list, iTunes-style.
        self._sources = QListWidget()
        self._sources.setMaximumWidth(200)
        for name in (SOURCE_ALL, SOURCE_RECENT, SOURCE_MOST_PLAYED):
            self._sources.addItem(name)
        self._sources.setCurrentRow(0)
        self._sources.currentRowChanged.connect(self._on_source_changed)

        self._artists = QListWidget()
        self._artists.currentItemChanged.connect(self._on_artist_changed)
        self._albums = QListWidget()
        self._albums.currentItemChanged.connect(lambda *_: self._refresh_tracks())

        browser = QSplitter(Qt.Horizontal)
        browser.addWidget(self._wrap_with_heading("Artists", self._artists))
        browser.addWidget(self._wrap_with_heading("Albums", self._albums))
        self._browser = browser

        self._model = TrackTableModel(self)
        self._proxy = _NaturalSortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(-1, Qt.AscendingOrder)  # library order until a header is clicked
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.doubleClicked.connect(self._on_track_activated)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_track_menu)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate((24, 40, 280, 180, 220, 56, 110, 60, 50, 80)):
            header.resizeSection(column, width)
        header.setStretchLastSection(True)

        self._empty_state = self._build_empty_state()
        self._track_stack = QStackedWidget()
        self._track_stack.addWidget(self._table)
        self._track_stack.addWidget(self._empty_state)

        right = QSplitter(Qt.Vertical)
        right.addWidget(browser)
        right.addWidget(self._track_stack)
        right.setSizes([200, 520])

        main_split = QSplitter(Qt.Horizontal)
        main_split.addWidget(self._sources)
        main_split.addWidget(right)
        main_split.setSizes([180, 1100])
        layout.addWidget(main_split, stretch=1)

        self.setCentralWidget(central)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._count_label = QLabel()
        self._status.addPermanentWidget(self._count_label)

    @staticmethod
    def _wrap_with_heading(title: str, widget: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 4, 0, 0)
        label = QLabel(title)
        label.setStyleSheet("font-weight: 600; padding-left: 4px;")
        layout.addWidget(label)
        layout.addWidget(widget)
        return box

    def _build_empty_state(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addStretch(1)
        message = QLabel("Your library is empty.\nAdd the folder where you keep your music to get started.")
        message.setAlignment(Qt.AlignCenter)
        message.setStyleSheet("color: gray; font-size: 15px;")
        layout.addWidget(message)
        button = QPushButton("Add Music Folder…")
        button.clicked.connect(self._add_music_folder)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        return box

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        add_action = QAction("&Add Music Folder…", self)
        add_action.setShortcut(QKeySequence("Ctrl+O"))
        add_action.triggered.connect(self._add_music_folder)
        file_menu.addAction(add_action)

        self._remove_folder_menu = file_menu.addMenu("&Remove Music Folder")
        self._remove_folder_menu.aboutToShow.connect(self._populate_remove_folder_menu)

        rescan_action = QAction("Re&scan Library", self)
        rescan_action.setShortcut(QKeySequence("F5"))
        rescan_action.triggered.connect(self._start_scan)
        file_menu.addAction(rescan_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        controls = self.menuBar().addMenu("&Controls")
        for text, shortcut, slot in (
            ("Play / Pause", "Space", self._toggle_play_pause),
            ("Next Track", "Ctrl+Right", lambda: self._play_next(user_requested=True)),
            ("Previous Track", "Ctrl+Left", self._play_previous),
            ("Find", "Ctrl+F", self._search.setFocus),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            controls.addAction(action)

    # --- background work ------------------------------------------------

    def _run_in_background(self, fn, on_finished) -> None:
        """Run fn() off the GUI thread; on_finished(result) runs back on it."""
        thread = QThread()
        worker = _CallableWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Explicit QueuedConnection so on_finished always runs on the GUI
        # thread, even when it's a plain lambda with no thread affinity.
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)

        def _cleanup():
            self._background_threads.remove(thread)
            self._background_workers.remove(worker)

        thread.finished.connect(_cleanup)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # Both must stay referenced from self, or Python can collect them
        # mid-flight.
        self._background_threads.append(thread)
        self._background_workers.append(worker)
        thread.start()

    # --- library folders & scanning -------------------------------------

    def _add_music_folder(self) -> None:
        start = str(Path.home() / "Music") if (Path.home() / "Music").is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Add Music Folder", start)
        if not folder:
            return
        if not self._settings.add_music_folder(folder):
            self._status.showMessage("That folder is already in your library", 4000)
            return
        self._start_scan()

    def _populate_remove_folder_menu(self) -> None:
        self._remove_folder_menu.clear()
        folders = self._settings.music_folders
        if not folders:
            placeholder = self._remove_folder_menu.addAction("No music folders")
            placeholder.setEnabled(False)
            return
        for folder in folders:
            action = self._remove_folder_menu.addAction(folder)
            action.triggered.connect(lambda _checked=False, f=folder: self._remove_music_folder(f))

    def _remove_music_folder(self, folder: str) -> None:
        self._settings.remove_music_folder(folder)
        removed = self._library.remove_folder(folder)
        self._status.showMessage(f"Removed {removed:,} tracks from the library", 5000)
        self._refresh_browser()

    def _start_scan(self) -> None:
        if self._scanning or not self._settings.music_folders:
            return
        self._scanning = True
        folders = self._settings.music_folders
        db_path = self._library.db_path
        bridge = _ScanProgressBridge()
        bridge.progress.connect(
            lambda count: self._status.showMessage(f"Scanning library… {count:,} files"),
            Qt.QueuedConnection,
        )
        self._scan_bridge = bridge

        def scan():
            # SQLite connections can't cross threads — the scan gets its own.
            library = Library(db_path)
            try:
                return library.scan(
                    folders,
                    progress=lambda count, _path: count % 50 == 0 and bridge.progress.emit(count),
                )
            finally:
                library.close()

        self._status.showMessage("Scanning library…")
        self._run_in_background(scan, self._on_scan_finished)

    def _on_scan_finished(self, result) -> None:
        self._scanning = False
        self._status.showMessage(
            f"Library up to date — {result.added:,} added, {result.updated:,} updated, "
            f"{result.removed:,} removed",
            6000,
        )
        self._refresh_browser()

    # --- browsing -------------------------------------------------------

    def _source(self) -> str:
        item = self._sources.currentItem()
        return item.text() if item else SOURCE_ALL

    def _selected(self, widget: QListWidget, all_label: str) -> Optional[str]:
        item = widget.currentItem()
        if item is None or item.text() == all_label:
            return None
        return item.data(Qt.UserRole)

    def _refresh_browser(self) -> None:
        """Repopulate artists/albums, keeping the current selections if they still exist."""
        artist = self._selected(self._artists, ALL_ARTISTS)
        self._artists.blockSignals(True)
        self._artists.clear()
        self._artists.addItem(ALL_ARTISTS)
        restore_row = 0
        for row, name in enumerate(self._library.artists(), start=1):
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self._artists.addItem(item)
            if name == artist:
                restore_row = row
        self._artists.setCurrentRow(restore_row)
        self._artists.blockSignals(False)
        self._refresh_albums()

    def _refresh_albums(self) -> None:
        album = self._selected(self._albums, ALL_ALBUMS)
        artist = self._selected(self._artists, ALL_ARTISTS)
        albums = self._library.albums(artist)
        self._albums.blockSignals(True)
        self._albums.clear()
        self._albums.addItem(f"{ALL_ALBUMS} ({len(albums)})")
        restore_row = 0
        for row, (album_artist, name, year) in enumerate(albums, start=1):
            label = name if artist else f"{name} — {album_artist}"
            if year:
                label += f" ({year})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            self._albums.addItem(item)
            if name == album:
                restore_row = row
        self._albums.setCurrentRow(restore_row)
        self._albums.blockSignals(False)
        self._refresh_tracks()

    def _on_source_changed(self, _row: int) -> None:
        self._browser.setVisible(self._source() == SOURCE_ALL)
        self._refresh_tracks()

    def _on_artist_changed(self, *_args) -> None:
        self._albums.setCurrentRow(0)
        self._refresh_albums()

    def _refresh_tracks(self) -> None:
        source = self._source()
        if source == SOURCE_RECENT:
            tracks = self._library.recently_added()
        elif source == SOURCE_MOST_PLAYED:
            tracks = self._library.most_played()
        else:
            album_item = self._albums.currentItem()
            album = None
            if album_item is not None and self._albums.currentRow() > 0:
                album = album_item.data(Qt.UserRole)
            tracks = self._library.tracks(
                artist=self._selected(self._artists, ALL_ARTISTS),
                album=album,
                search=self._search.text(),
            )
        search = self._search.text().lower().split()
        if source != SOURCE_ALL and search:
            tracks = [
                t for t in tracks
                if all(w in f"{t.title} {t.artist} {t.album} {t.genre}".lower() for w in search)
            ]
        self._model.set_tracks(tracks)
        self._model.set_playing(self._current.id if self._current else None)

        total = self._library.track_count()
        self._track_stack.setCurrentWidget(self._table if total else self._empty_state)
        seconds = sum(t.duration for t in tracks)
        hours = seconds / 3600
        length = f"{hours:.1f} hours" if hours >= 1 else f"{int(seconds // 60)} minutes"
        self._count_label.setText(f"{len(tracks):,} songs, {length}")

    def _visible_tracks(self) -> list[Track]:
        """Tracks in the order they're shown (respecting any column sort)."""
        return [
            self._model.track_at(self._proxy.mapToSource(self._proxy.index(row, 0)).row())
            for row in range(self._proxy.rowCount())
        ]

    def _show_track_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        track = self._model.track_at(self._proxy.mapToSource(index).row())
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._on_track_activated(index))
        rating_menu = menu.addMenu("Rating")
        for stars in range(6):
            label = "★" * stars if stars else "None"
            rating_menu.addAction(label, lambda s=stars: self._rate(track, s))
        menu.addAction(
            "Show in Folder",
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(track.path).parent))),
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _rate(self, track: Track, stars: int) -> None:
        self._library.set_rating(track.id, stars)
        updated = self._library.track(track.id)
        if updated:
            self._model.replace_track(updated)

    # --- playback -------------------------------------------------------

    def _on_track_activated(self, index) -> None:
        row = index.row()  # proxy row == position in _visible_tracks()
        tracks = self._visible_tracks()
        track_id = self._queue.load([t.id for t in tracks], start_index=row)
        self._play_track_id(track_id)

    def _play_track_id(self, track_id: Optional[int]) -> None:
        if track_id is None:
            self._stop()
            return
        track = self._library.track(track_id)
        if track is None or not Path(track.path).exists():
            self._status.showMessage("That file is missing — skipping", 4000)
            QTimer.singleShot(0, lambda: self._play_next(user_requested=True))
            return
        self._current = track
        self._play_recorded = False
        self._player.setSource(QUrl.fromLocalFile(track.path))
        self._player.play()
        self._player_bar.set_active(True)
        subtitle = " — ".join(part for part in (track.artist, track.album) if part)
        self._player_bar.set_now_playing(track.title, subtitle)
        self._player_bar.set_art(find_art(Path(track.path)))
        self._model.set_playing(track.id)
        self.setWindowTitle(f"{track.title} — {track.artist} · Simple-Jukebox")

    def _toggle_play_pause(self) -> None:
        if self._focus_is_text_entry():
            return
        if self._current is None:
            tracks = self._visible_tracks()
            if tracks:
                selected = self._table.currentIndex()
                start = selected.row() if selected.isValid() else 0
                self._play_track_id(self._queue.load([t.id for t in tracks], start_index=start))
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _focus_is_text_entry(self) -> bool:
        return self._search.hasFocus()

    def _play_next(self, user_requested: bool = False) -> None:
        self._play_track_id(self._queue.next(user_requested=user_requested))

    def _play_previous(self) -> None:
        # Like every music player: "previous" restarts the song unless
        # you're right at its start.
        if self._player.position() > 3000:
            self._player.setPosition(0)
            return
        self._play_track_id(self._queue.previous())

    def _stop(self) -> None:
        self._player.stop()
        self._current = None
        self._player_bar.set_active(False)
        self._model.set_playing(None)
        self.setWindowTitle(f"Simple-Jukebox v{__version__}")

    def _on_position_changed(self, position_ms: int) -> None:
        duration_ms = self._player.duration()
        self._player_bar.set_progress(position_ms, duration_ms)
        if (
            self._current is not None
            and not self._play_recorded
            and duration_ms > 0
            and position_ms >= duration_ms * PLAYED_FRACTION
        ):
            self._play_recorded = True
            self._library.record_play(self._current.id)
            updated = self._library.track(self._current.id)
            if updated:
                self._model.replace_track(updated)

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia:
            self._play_next()

    def _on_playback_state_changed(self, state) -> None:
        self._player_bar.set_playing(state == QMediaPlayer.PlayingState)

    def _on_player_error(self, _error, error_string: str) -> None:
        if self._current is not None:
            self._status.showMessage(f"Couldn't play {self._current.title}: {error_string}", 6000)

    def _on_volume_changed(self, volume: int) -> None:
        self._audio_output.setVolume(volume / 100)
        self._settings.set_volume(volume)

    def _on_shuffle_toggled(self, shuffle: bool) -> None:
        self._queue.set_shuffle(shuffle)
        self._settings.set_shuffle(shuffle)

    def _on_repeat_changed(self, repeat: str) -> None:
        self._queue.repeat = repeat
        self._settings.set_repeat(repeat)

    # --- updates --------------------------------------------------------

    def _check_for_updates(self) -> None:
        self._run_in_background(
            lambda: check_for_update(__version__, UPDATE_OWNER, UPDATE_REPO),
            self._on_update_checked,
        )

    def _on_update_checked(self, info) -> None:
        if info:
            self._pending_update = info
            self._update_banner.announce(info.version)

    def _start_update(self) -> None:
        if self._pending_update is None:
            return
        asset_url = find_asset_for_this_platform(self._pending_update.assets)
        if asset_url is None:
            self._update_banner.set_status(
                f"No installer for this platform — see {self._pending_update.url}"
            )
            return
        self._update_banner.set_busy(True, "Downloading…")
        self._run_in_background(lambda: download_asset(asset_url), self._on_update_downloaded)

    def _on_update_downloaded(self, path) -> None:
        if path is None:
            self._update_banner.set_status("Couldn't download the update — try again")
            self._update_banner.set_busy(False)
            return
        try:
            process = launch_installer(path)
        except OSError:
            self._update_banner.set_status(f"Downloaded to {path} — open it manually")
            self._update_banner.set_busy(False)
            return

        self._update_banner.set_status("Installer launched — closing to finish…")
        if sys.platform not in ("win32", "darwin") and process is not None:
            # Linux: wait for pkexec/apt to finish before quitting — quitting
            # immediately can kill its password prompt before it's answered.
            self._run_in_background(
                lambda: (path, self._wait_for_installer(process)),
                self._on_installer_finished,
            )
        else:
            QTimer.singleShot(1500, self.close)

    @staticmethod
    def _wait_for_installer(process) -> tuple[int, str]:
        returncode = process.wait()
        stderr_text = process.stderr.read().strip() if process.stderr is not None else ""
        return returncode, stderr_text

    def _on_installer_finished(self, result) -> None:
        path, (returncode, stderr_text) = result
        if returncode == 0:
            self.close()
            return
        detail = f": {stderr_text}" if stderr_text else f" (exit code {returncode})"
        self._update_banner.set_status(f"Update didn't install{detail} — installer is at {path}")
        self._update_banner.set_busy(False)

    def closeEvent(self, event) -> None:
        self._player.stop()
        for thread in list(self._background_threads):
            thread.quit()
            thread.wait(BACKGROUND_JOIN_TIMEOUT_MS)
        self._library.close()
        super().closeEvent(event)
