from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PySide6.QtCore import QItemSelectionModel, QObject, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
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
from simple_jukebox.core.diagnostics import note_current_track
from simple_jukebox.core.library import Library, Track, already_in_playlist
from simple_jukebox.core.play_queue import PlayQueue
from simple_jukebox.core.self_update import (
    download_asset,
    find_asset_for_this_platform,
    launch_installer,
)
from simple_jukebox.core.settings import SettingsStore
from simple_jukebox.core.updater import API_TIMEOUT_SECONDS, check_for_update
from simple_jukebox.gui.album_grid import AlbumGridModel, AlbumGridView
from simple_jukebox.gui.player_bar import PlayerBar
from simple_jukebox.gui.podcast_manager import PodcastManager
from simple_jukebox.gui.podcasts_page import PodcastsPage
from simple_jukebox.gui.subscribe_dialog import SubscribeDialog
from simple_jukebox.gui.sync_dialog import SyncDialog
from simple_jukebox.gui.source_list import KIND_LIBRARY, KIND_PLAYLIST, SourceList
from simple_jukebox.gui.track_model import COLUMNS, RATING_COLUMN, TrackTableModel
from simple_jukebox.gui.up_next_panel import UpNextPanel
from simple_jukebox.gui.update_banner import UpdateBanner

UPDATE_OWNER = "mikehellyer"
UPDATE_REPO = "simple-jukebox"
BACKGROUND_JOIN_TIMEOUT_MS = (API_TIMEOUT_SECONDS + 1) * 1000
ICON_PATH = Path(__file__).parent / "resources" / "icon.png"

SOURCE_ALL = "All Music"
SOURCE_ALBUMS = "Albums"
SOURCE_RECENT = "Recently Added"
SOURCE_MOST_PLAYED = "Most Played"
SOURCE_PODCASTS = "Podcasts"

PODCAST_SKIP_BACK_MS = 15_000
PODCAST_SKIP_FORWARD_MS = 30_000
PODCAST_POSITION_SAVE_MS = 5_000
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
        self._current_episode = None  # a podcast Episode, when one is playing
        self._pending_seek_ms = 0  # where to resume an episode once it has loaded
        self._last_position_save_ms = 0
        self._podcasts = PodcastManager(self._settings, parent=self)
        self._play_recorded = False
        self._scanning = False
        self._background_threads: list[QThread] = []
        self._background_workers: list[_CallableWorker] = []
        self._pending_update = None
        self._closing = False

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

        self._refresh_playlists()
        self._refresh_browser()
        if self._settings.music_folders:
            self._start_scan()
        self._check_for_updates()
        # Look for new episodes shortly after startup, once the window is up.
        QTimer.singleShot(3000, self._podcasts.refresh_all)

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
        self._player_bar.previous_requested.connect(self._on_previous_clicked)
        self._player_bar.next_requested.connect(self._on_next_clicked)
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
        self._search.setMinimumWidth(120)
        self._search.setMaximumWidth(240)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._on_search_changed)
        self._search.textChanged.connect(lambda _text: self._search_timer.start())
        top_row.addWidget(self._search)
        self._up_next_button = QPushButton("☰ Up Next")
        self._up_next_button.setCheckable(True)
        top_row.addWidget(self._up_next_button)
        layout.addLayout(top_row)

        # Left: library sources. Right: artist/album column browser above
        # the track list, iTunes-style.
        self._sources = SourceList([SOURCE_ALL, SOURCE_ALBUMS, SOURCE_RECENT, SOURCE_MOST_PLAYED, SOURCE_PODCASTS])
        self._sources.currentRowChanged.connect(self._on_source_changed)
        self._sources.tracks_dropped_on_playlist.connect(self._add_to_playlist)
        self._sources.itemDoubleClicked.connect(lambda _item: self._play_source())
        self._sources.setContextMenuPolicy(Qt.CustomContextMenu)
        self._sources.customContextMenuRequested.connect(self._show_source_menu)
        new_playlist_button = QPushButton("＋ New Playlist")
        new_playlist_button.setFlat(True)
        new_playlist_button.clicked.connect(lambda: self._new_playlist())
        sidebar = QWidget()
        sidebar.setMaximumWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 4)
        sidebar_layout.addWidget(self._sources, stretch=1)
        sidebar_layout.addWidget(new_playlist_button)

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
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDrop)
        self._table.setDefaultDropAction(Qt.MoveAction)
        self._table.setDropIndicatorShown(True)
        self._model.rows_moved.connect(self._on_playlist_rows_moved)
        self._table.horizontalHeader().sortIndicatorChanged.connect(lambda *_: self._update_reorderable())
        for key in (QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)):
            shortcut = QShortcut(key, self._table)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(self._remove_selected_from_playlist)
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
        # Clicking a header cycles ascending → descending → back to the
        # list's own order (album order, or a playlist's running order).
        header.setSortIndicatorClearable(True)

        self._empty_state = self._build_empty_state()
        self._track_stack = QStackedWidget()
        self._track_stack.addWidget(self._table)
        self._track_stack.addWidget(self._empty_state)

        # The album grid takes the browser's place (and more room) when
        # "Albums" is chosen; picking an album lists its songs below.
        self._album_model = AlbumGridModel(self._library, self)
        self._album_grid = AlbumGridView()
        self._album_grid.setModel(self._album_model)
        self._album_grid.selectionModel().selectionChanged.connect(lambda *_: self._refresh_tracks())
        self._album_grid.doubleClicked.connect(lambda index: self._play_albums([index.row()]))
        self._album_grid.customContextMenuRequested.connect(self._show_album_menu)
        self._album_grid.hide()

        right = QSplitter(Qt.Vertical)
        right.addWidget(browser)
        right.addWidget(self._album_grid)
        right.addWidget(self._track_stack)
        right.setSizes([200, 0, 520])
        self._right_split = right

        # Podcasts get a page of their own in place of the music views.
        self._podcasts_page = PodcastsPage(self._podcasts)
        self._podcasts_page.play_requested.connect(self._play_episode)
        self._podcasts_page.subscribe_requested.connect(self._subscribe_to_podcast)
        self._podcasts.status.connect(lambda text: self._status.showMessage(text, 6000))
        self._right_stack = QStackedWidget()
        self._right_stack.addWidget(right)
        self._right_stack.addWidget(self._podcasts_page)

        main_split = QSplitter(Qt.Horizontal)
        main_split.addWidget(sidebar)
        main_split.addWidget(self._right_stack)
        main_split.setSizes([180, 1100])
        layout.addWidget(main_split, stretch=1)

        self.setCentralWidget(central)

        self._up_next = UpNextPanel()
        self._up_next.jump_requested.connect(self._jump_in_queue)
        self._up_next.remove_requested.connect(lambda offset: self._edit_queue(self._queue.remove_upcoming, offset))
        self._up_next.move_requested.connect(lambda a, b: self._edit_queue(self._queue.move_upcoming, a, b))
        self._up_next.tracks_dropped.connect(self._on_tracks_dropped_on_up_next)
        self._up_next.clear_requested.connect(lambda: self._edit_queue(self._queue.clear_upcoming))
        self._up_next_dock = QDockWidget("Up Next", self)
        self._up_next_dock.setObjectName("up-next")
        self._up_next_dock.setWidget(self._up_next)
        self._up_next_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self._up_next_dock.setMinimumWidth(240)
        self.addDockWidget(Qt.RightDockWidgetArea, self._up_next_dock)
        self._up_next_dock.setVisible(self._settings.show_up_next)
        self._up_next_button.setChecked(self._settings.show_up_next)
        self._up_next_button.toggled.connect(self._up_next_dock.setVisible)
        self._up_next_dock.visibilityChanged.connect(self._on_up_next_visibility)

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

        new_playlist_action = QAction("&New Playlist…", self)
        new_playlist_action.setShortcut(QKeySequence.New)
        new_playlist_action.triggered.connect(lambda: self._new_playlist())
        file_menu.addAction(new_playlist_action)
        file_menu.addSeparator()

        rescan_action = QAction("Re&scan Library", self)
        rescan_action.setShortcut(QKeySequence("F5"))
        rescan_action.triggered.connect(self._start_scan)
        file_menu.addAction(rescan_action)

        file_menu.addSeparator()
        subscribe_action = QAction("Subscribe to &Podcast…", self)
        subscribe_action.setShortcut(QKeySequence("Ctrl+Shift+P"))
        subscribe_action.triggered.connect(self._subscribe_to_podcast)
        file_menu.addAction(subscribe_action)
        refresh_podcasts_action = QAction("Refresh Podcasts", self)
        refresh_podcasts_action.triggered.connect(self._podcasts.refresh_all)
        file_menu.addAction(refresh_podcasts_action)
        file_menu.addSeparator()

        sync_action = QAction("Sync to &Device…", self)
        sync_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        sync_action.triggered.connect(self._open_sync_dialog)
        file_menu.addAction(sync_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        toggle_up_next = QAction("Show &Up Next", self)
        toggle_up_next.setCheckable(True)
        toggle_up_next.setShortcut(QKeySequence("Ctrl+U"))
        toggle_up_next.setChecked(self._settings.show_up_next)
        toggle_up_next.toggled.connect(self._up_next_dock.setVisible)
        self._up_next_dock.visibilityChanged.connect(toggle_up_next.setChecked)
        view_menu.addAction(toggle_up_next)

        controls = self.menuBar().addMenu("&Controls")
        for text, shortcut, slot in (
            ("Play / Pause", "Space", self._toggle_play_pause),
            ("Next Track", "Ctrl+Right", self._on_next_clicked),
            ("Previous Track", "Ctrl+Left", self._on_previous_clicked),
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
        if self._album_grid.isVisible():
            self._refresh_album_grid()

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
        if result.added or result.updated or result.removed:
            self._album_model.forget_art()  # covers may have changed
        self._refresh_browser()
        if self._album_grid.isVisible():
            self._refresh_album_grid()

    # --- browsing -------------------------------------------------------

    def _source(self) -> tuple[str, object]:
        return self._sources.current_source() or (KIND_LIBRARY, SOURCE_ALL)

    def _playlist_id(self) -> Optional[int]:
        kind, value = self._source()
        return value if kind == KIND_PLAYLIST else None

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
        podcasts = self._source() == (KIND_LIBRARY, SOURCE_PODCASTS)
        self._right_stack.setCurrentWidget(self._podcasts_page if podcasts else self._right_split)
        if podcasts:
            self._podcasts_page.set_filter(self._search.text())
            self._count_label.setText("")
            return
        self._browser.setVisible(self._source() == (KIND_LIBRARY, SOURCE_ALL))
        albums = self._source() == (KIND_LIBRARY, SOURCE_ALBUMS)
        was_showing_albums = self._album_grid.isVisible()
        self._album_grid.setVisible(albums)
        if albums:
            self._refresh_album_grid()
            if not was_showing_albums:
                total = sum(self._right_split.sizes())
                self._right_split.setSizes([0, int(total * 0.62), int(total * 0.38)])
        # Playlists open in their own order, not whatever column the
        # library was last sorted by.
        if self._playlist_id() is not None:
            self._table.sortByColumn(-1, Qt.AscendingOrder)
        self._refresh_tracks()

    def _on_search_changed(self) -> None:
        if self._right_stack.currentWidget() is self._podcasts_page:
            self._podcasts_page.set_filter(self._search.text())
            return
        if self._album_grid.isVisible():
            self._refresh_album_grid()
        self._refresh_tracks()

    # --- podcasts -------------------------------------------------------

    def _subscribe_to_podcast(self) -> None:
        dialog = SubscribeDialog(self._run_in_background, self)
        if dialog.exec() and dialog.feed_url:
            self._sources.select_source(KIND_LIBRARY, SOURCE_PODCASTS)
            self._podcasts.subscribe(dialog.feed_url)

    def _play_episode(self, episode_id: int) -> None:
        episode = self._podcasts.episode(episode_id)
        if episode is None:
            return
        self._save_episode_position()
        self._current = None
        self._model.set_playing(None)
        self._current_episode = episode
        self._pending_seek_ms = 0 if episode.played else episode.position_ms
        self._last_position_save_ms = self._pending_seek_ms
        if episode.downloaded:
            source = QUrl.fromLocalFile(episode.local_path)
            art = find_art(Path(episode.local_path)) or self._podcasts.artwork_bytes(episode.podcast_id)
        else:
            source = QUrl(episode.audio_url)
            art = self._podcasts.artwork_bytes(episode.podcast_id)
            self._status.showMessage("Streaming — download the episode to listen offline", 5000)
        note_current_track(episode.local_path or episode.audio_url)
        self._player.setSource(source)
        self._player.play()
        self._player_bar.set_active(True)
        self._player_bar.set_podcast_mode(True)
        self._player_bar.set_now_playing(episode.title, episode.podcast_title)
        self._player_bar.set_art(art)
        self._refresh_up_next()

    def _save_episode_position(self) -> None:
        if self._current_episode is not None:
            position = self._player.position()
            if position > 0:
                self._podcasts.save_position(self._current_episode.id, position)

    def _leave_episode(self) -> None:
        """Before playing something else: remember where the episode got to."""
        if self._current_episode is not None:
            self._save_episode_position()
            self._current_episode = None
            self._pending_seek_ms = 0
            self._player_bar.set_podcast_mode(False)
            self._podcasts.changed.emit()

    def _after_episode(self) -> None:
        # An episode that finishes continues into Up Next, if anything's queued.
        if self._queue.upcoming():
            self._play_next(user_requested=True)
        else:
            self._stop()

    def _on_previous_clicked(self) -> None:
        if self._current_episode is not None:
            self._player.setPosition(max(0, self._player.position() - PODCAST_SKIP_BACK_MS))
        else:
            self._play_previous()

    def _on_next_clicked(self) -> None:
        if self._current_episode is not None:
            duration = self._player.duration()
            target = self._player.position() + PODCAST_SKIP_FORWARD_MS
            self._player.setPosition(min(target, duration - 1000) if duration > 0 else target)
        else:
            self._play_next(user_requested=True)

    # --- album grid -----------------------------------------------------

    def _refresh_album_grid(self) -> None:
        """Repopulate the grid for the current search, keeping the
        selected albums selected if they're still there."""
        selected = {album.key for album in self._selected_albums()}
        self._album_grid.selectionModel().blockSignals(True)
        self._album_model.set_albums(self._library.album_summaries(self._search.text()))
        selection = self._album_grid.selectionModel()
        first = None
        for key in selected:
            row = self._album_model.row_of(key)
            if row is not None:
                index = self._album_model.index(row)
                selection.select(index, QItemSelectionModel.Select)
                first = index if first is None or row < first.row() else first
        if first is not None:
            selection.setCurrentIndex(first, QItemSelectionModel.NoUpdate)
            self._album_grid.scrollTo(first)
        self._album_grid.selectionModel().blockSignals(False)

    def _selected_albums(self):
        rows = sorted({index.row() for index in self._album_grid.selectionModel().selectedIndexes()})
        return [album for album in (self._album_model.album_at(row) for row in rows) if album is not None]

    def _album_track_ids(self, rows: list[int]) -> list[int]:
        ids = []
        for row in rows:
            album = self._album_model.album_at(row)
            if album is not None:
                ids.extend(t.id for t in self._library.album_tracks(*album.key))
        return ids

    def _play_albums(self, rows: list[int]) -> None:
        track_ids = self._album_track_ids(rows)
        if track_ids:
            self._play_track_id(self._queue.load(track_ids))

    def _show_album_menu(self, pos) -> None:
        index = self._album_grid.indexAt(pos)
        if not index.isValid():
            return
        selection = self._album_grid.selectionModel()
        if not selection.isSelected(index):
            selection.select(index, QItemSelectionModel.ClearAndSelect)
        rows = sorted({i.row() for i in selection.selectedIndexes()})
        track_ids = self._album_track_ids(rows)
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_albums(rows))
        menu.addAction("Play Next", lambda: self._queue_tracks(track_ids, play_next=True))
        menu.addAction("Add to Up Next", lambda: self._queue_tracks(track_ids, play_next=False))
        menu.addSeparator()
        playlist_menu = menu.addMenu("Add to Playlist")
        playlist_menu.addAction("New Playlist…", lambda: self._new_playlist(track_ids))
        playlists = self._library.playlists()
        if playlists:
            playlist_menu.addSeparator()
        for playlist in playlists:
            playlist_menu.addAction(
                playlist.name, lambda pid=playlist.id: self._add_to_playlist(pid, track_ids)
            )
        if len(rows) == 1:
            album = self._album_model.album_at(rows[0])
            menu.addSeparator()
            menu.addAction(
                "Show in Folder",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(album.cover_path).parent))),
            )
        menu.exec(self._album_grid.viewport().mapToGlobal(pos))

    def _on_artist_changed(self, *_args) -> None:
        self._albums.setCurrentRow(0)
        self._refresh_albums()

    def _refresh_tracks(self) -> None:
        kind, source = self._source()
        if (kind, source) == (KIND_LIBRARY, SOURCE_PODCASTS):
            return  # the podcasts page keeps itself up to date
        if kind == KIND_PLAYLIST:
            tracks = self._library.playlist_tracks(source)
        elif source == SOURCE_RECENT:
            tracks = self._library.recently_added()
        elif source == SOURCE_MOST_PLAYED:
            tracks = self._library.most_played()
        elif source == SOURCE_ALBUMS:
            tracks = []
            for album in self._selected_albums():
                tracks.extend(self._library.album_tracks(*album.key))
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
        if (kind, source) != (KIND_LIBRARY, SOURCE_ALL) and search:
            tracks = [
                t for t in tracks
                if all(w in f"{t.title} {t.artist} {t.album} {t.genre}".lower() for w in search)
            ]
        self._model.set_tracks(tracks)
        self._model.set_playing(self._current.id if self._current else None)
        self._update_reorderable()

        total = self._library.track_count()
        # A playlist can legitimately be empty — only the library as a
        # whole being empty gets the "add a folder" screen.
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

    def _selected_tracks(self) -> list[tuple[int, Track]]:
        """(source row, track) for each selected row, in on-screen order.
        In a playlist view the source row is the entry's playlist position."""
        proxy_rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        selected = []
        for proxy_row in proxy_rows:
            source_row = self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()
            selected.append((source_row, self._model.track_at(source_row)))
        return selected

    def _show_track_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        selection = self._table.selectionModel()
        if not selection.isRowSelected(index.row(), index.parent()):
            self._table.selectRow(index.row())
        selected = self._selected_tracks()
        track_ids = [track.id for _, track in selected]
        first = selected[0][1]

        menu = QMenu(self)
        menu.addAction("Play", lambda: self._on_track_activated(index))
        menu.addAction("Play Next", lambda: self._queue_tracks(track_ids, play_next=True))
        menu.addAction("Add to Up Next", lambda: self._queue_tracks(track_ids, play_next=False))
        menu.addSeparator()

        playlist_menu = menu.addMenu("Add to Playlist")
        playlist_menu.addAction("New Playlist…", lambda: self._new_playlist(track_ids))
        playlists = self._library.playlists()
        if playlists:
            playlist_menu.addSeparator()
        for playlist in playlists:
            playlist_menu.addAction(
                playlist.name, lambda pid=playlist.id: self._add_to_playlist(pid, track_ids)
            )
        if self._playlist_id() is not None:
            menu.addAction("Remove from Playlist", self._remove_selected_from_playlist)
        menu.addSeparator()

        rating_menu = menu.addMenu("Rating")
        for stars in range(6):
            label = "★" * stars if stars else "None"
            rating_menu.addAction(label, lambda s=stars: [self._rate(track, s) for _, track in selected])
        menu.addAction(
            "Show in Folder",
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(first.path).parent))),
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # --- devices --------------------------------------------------------

    def _open_sync_dialog(self) -> None:
        SyncDialog(
            self._library, self._settings, self._run_in_background, podcast_store=self._podcasts.store, parent=self
        ).exec()

    # --- playlists ------------------------------------------------------

    def _refresh_playlists(self) -> None:
        self._sources.blockSignals(True)
        self._sources.set_playlists(self._library.playlists())
        self._sources.blockSignals(False)

    def _new_playlist(self, track_ids: Optional[list[int]] = None) -> None:
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:", text="Untitled Playlist")
        if not ok:
            return
        playlist_id = self._library.create_playlist(name)
        if track_ids:
            self._library.add_to_playlist(playlist_id, track_ids)
        self._refresh_playlists()
        if not track_ids:
            # An empty playlist: open it, ready to drag songs onto it.
            self._sources.select_source(KIND_PLAYLIST, playlist_id)

    def _add_to_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        name = next((p.name for p in self._library.playlists() if p.id == playlist_id), "playlist")
        duplicates = already_in_playlist(self._library.playlist_track_ids(playlist_id), track_ids)
        skipped = 0
        if duplicates:
            kept = self._ask_about_playlist_duplicates(name, track_ids, duplicates)
            if kept is None:
                self._status.showMessage(f"Nothing added to {name}", 4000)
                return
            skipped = len(track_ids) - len(kept)
            track_ids = kept

        def songs(count: int) -> str:
            return f"{count} song{'s' if count != 1 else ''}"

        if not track_ids:
            self._status.showMessage(f"Nothing added — already in {name}", 4000)
            return
        self._library.add_to_playlist(playlist_id, track_ids)
        self._refresh_playlists()
        message = f"Added {songs(len(track_ids))} to {name}"
        if skipped:
            message += f" (skipped {songs(skipped)} already in it)"
        self._status.showMessage(message, 5000)
        if self._playlist_id() == playlist_id:
            self._refresh_tracks()

    def _ask_about_playlist_duplicates(
        self, playlist_name: str, track_ids: list[int], duplicates: list[int]
    ) -> Optional[list[int]]:
        """Ask, song by song, whether to add songs already in the playlist
        again — with an "apply to the rest" option when there are several.
        Returns the ids to add, or None if the user cancelled the whole add."""
        tracks = self._library.tracks_by_ids(track_ids)
        skip: set[int] = set()  # indexes into track_ids
        decision_for_rest: Optional[bool] = None  # True = add again, False = skip
        for position, index in enumerate(duplicates):
            if decision_for_rest is None:
                track = tracks.get(track_ids[index])
                song = f"“{track.title}” by {track.artist}" if track else "This song"
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle("Already in Playlist")
                box.setText(f"{song} is already in “{playlist_name}”.")
                box.setInformativeText("Add it again?")
                add_button = box.addButton("Add Again", QMessageBox.YesRole)
                skip_button = box.addButton("Skip", QMessageBox.NoRole)
                box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(skip_button)
                remaining = len(duplicates) - position - 1
                apply_to_rest = None
                if remaining:
                    apply_to_rest = QCheckBox(
                        f"Do the same for the other {remaining} song{'s' if remaining != 1 else ''} "
                        "already in this playlist"
                    )
                    box.setCheckBox(apply_to_rest)
                box.exec()
                clicked = box.clickedButton()
                if clicked not in (add_button, skip_button):
                    return None  # Cancel, Escape or the window's close button
                add_again = clicked is add_button
                if apply_to_rest is not None and apply_to_rest.isChecked():
                    decision_for_rest = add_again
            else:
                add_again = decision_for_rest
            if not add_again:
                skip.add(index)
        return [track_id for index, track_id in enumerate(track_ids) if index not in skip]

    def _remove_selected_from_playlist(self) -> None:
        playlist_id = self._playlist_id()
        if playlist_id is None:
            return
        positions = [row for row, _ in self._selected_tracks()]
        if not positions:
            return
        self._library.remove_from_playlist(playlist_id, positions)
        self._refresh_playlists()
        self._refresh_tracks()

    def _update_reorderable(self) -> None:
        """Drag-to-reorder only makes sense in a playlist shown in its own
        order — not when sorted by a column, or while searching."""
        self._model.reorderable = (
            self._playlist_id() is not None
            and self._table.horizontalHeader().sortIndicatorSection() == -1
            and not self._search.text().strip()
        )

    def _on_playlist_rows_moved(self, rows: list, to_row: int) -> None:
        playlist_id = self._playlist_id()
        if playlist_id is None:
            return
        self._library.move_in_playlist(playlist_id, rows, to_row)
        self._refresh_tracks()
        # Keep the moved songs selected where they landed.
        insert_at = to_row - sum(1 for r in rows if r < to_row)
        self._table.clearSelection()
        for row in range(insert_at, insert_at + len(rows)):
            self._table.selectionModel().select(
                self._proxy.index(row, 0),
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )

    def _show_source_menu(self, pos) -> None:
        playlist_id = self._sources.playlist_at(pos)
        if playlist_id is None:
            return
        playlist = next((p for p in self._library.playlists() if p.id == playlist_id), None)
        if playlist is None:
            return
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_playlist(playlist_id))
        menu.addAction("Add to Up Next", lambda: self._queue_tracks(
            [t.id for t in self._library.playlist_tracks(playlist_id)], play_next=False))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self._rename_playlist(playlist))
        menu.addAction("Delete", lambda: self._delete_playlist(playlist))
        menu.exec(self._sources.viewport().mapToGlobal(pos))

    def _rename_playlist(self, playlist) -> None:
        name, ok = QInputDialog.getText(self, "Rename Playlist", "Playlist name:", text=playlist.name)
        if ok:
            self._library.rename_playlist(playlist.id, name)
            self._refresh_playlists()

    def _delete_playlist(self, playlist) -> None:
        answer = QMessageBox.question(
            self,
            "Delete Playlist",
            f"Delete the playlist “{playlist.name}”?\nThe songs stay in your library.",
        )
        if answer != QMessageBox.Yes:
            return
        was_open = self._playlist_id() == playlist.id
        self._library.delete_playlist(playlist.id)
        self._refresh_playlists()
        if was_open:
            self._sources.select_source(KIND_LIBRARY, SOURCE_ALL)

    def _play_playlist(self, playlist_id: int) -> None:
        track_ids = [t.id for t in self._library.playlist_tracks(playlist_id)]
        if track_ids:
            self._play_track_id(self._queue.load(track_ids))

    def _play_source(self) -> None:
        playlist_id = self._playlist_id()
        if playlist_id is not None:
            self._play_playlist(playlist_id)

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

    def _queue_tracks(self, track_ids: list[int], play_next: bool) -> None:
        if play_next:
            self._queue.play_next(track_ids)
        else:
            self._queue.add(track_ids)
        self._refresh_up_next()
        count = len(track_ids)
        where = "to play next" if play_next else "to Up Next"
        self._status.showMessage(f"Queued {count} song{'s' if count != 1 else ''} {where}", 4000)

    def _on_tracks_dropped_on_up_next(self, track_ids: list, offset: int) -> None:
        self._queue.add(track_ids)
        # add() puts them at the end; slide each into place in order.
        upcoming = len(self._queue.upcoming())
        for i in range(len(track_ids)):
            self._queue.move_upcoming(upcoming - len(track_ids) + i, offset + i)
        self._refresh_up_next()

    def _edit_queue(self, edit, *args) -> None:
        edit(*args)
        self._refresh_up_next()

    def _jump_in_queue(self, offset: int) -> None:
        self._play_track_id(self._queue.jump_to(offset))

    def _refresh_up_next(self) -> None:
        upcoming_ids = self._queue.upcoming()
        found = self._library.tracks_by_ids(upcoming_ids[:500])
        now_playing = self._current
        if self._current_episode is not None:
            # The panel only needs a title and a second line.
            now_playing = SimpleNamespace(
                title=self._current_episode.title, artist=self._current_episode.podcast_title
            )
        upcoming = [found.get(track_id) for track_id in upcoming_ids[:500]]
        # Only the shown head needs real Track objects; the rest is counted.
        upcoming += [None] * (len(upcoming_ids) - len(upcoming))
        self._up_next.set_queue(now_playing, upcoming)

    def _on_up_next_visibility(self, visible: bool) -> None:
        self._up_next_button.setChecked(visible)
        # visibilityChanged also fires when the window is minimised —
        # only remember real show/hide choices.
        if not self._closing and not self.isMinimized() and self.isVisible():
            self._settings.set_show_up_next(visible)

    def _play_track_id(self, track_id: Optional[int]) -> None:
        if track_id is None:
            self._stop()
            return
        track = self._library.track(track_id)
        if track is None or not Path(track.path).exists():
            self._status.showMessage("That file is missing — skipping", 4000)
            QTimer.singleShot(0, lambda: self._play_next(user_requested=True))
            return
        self._leave_episode()
        self._current = track
        self._play_recorded = False
        note_current_track(track.path)
        self._player.setSource(QUrl.fromLocalFile(track.path))
        self._player.play()
        self._player_bar.set_active(True)
        subtitle = " — ".join(part for part in (track.artist, track.album) if part)
        self._player_bar.set_now_playing(track.title, subtitle)
        self._player_bar.set_art(find_art(Path(track.path)))
        self._model.set_playing(track.id)
        self._refresh_up_next()

    def _toggle_play_pause(self) -> None:
        if self._focus_is_text_entry():
            return
        if self._current is None and self._current_episode is None:
            if self._queue.upcoming():
                self._play_next(user_requested=True)
                return
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
        self._leave_episode()
        self._player.stop()
        self._current = None
        self._player_bar.set_active(False)
        self._model.set_playing(None)
        self._refresh_up_next()

    def _on_position_changed(self, position_ms: int) -> None:
        duration_ms = self._player.duration()
        self._player_bar.set_progress(position_ms, duration_ms)
        if (
            self._current_episode is not None
            and not self._pending_seek_ms
            and abs(position_ms - self._last_position_save_ms) >= PODCAST_POSITION_SAVE_MS
        ):
            self._last_position_save_ms = position_ms
            self._podcasts.save_position(self._current_episode.id, position_ms)
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
        if (
            self._pending_seek_ms
            and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia)
            and self._player.isSeekable()
        ):
            # Resume a part-heard episode where it was left off.
            self._player.setPosition(self._pending_seek_ms)
            self._pending_seek_ms = 0
        if status == QMediaPlayer.EndOfMedia and self._current_episode is not None:
            self._podcasts.finished_playing(self._current_episode.id)
            self._current_episode = None
            self._player_bar.set_podcast_mode(False)
            QTimer.singleShot(0, self._after_episode)
            return
        if status == QMediaPlayer.EndOfMedia:
            # Don't swap the player's source from inside its own
            # end-of-media signal — the backend is still finishing that
            # track when this runs, and changing source re-entrantly can
            # leave it replaying the old track or stuck. Advance once
            # control is back in the event loop instead.
            QTimer.singleShot(0, self._play_next)

    def _on_playback_state_changed(self, state) -> None:
        self._player_bar.set_playing(state == QMediaPlayer.PlayingState)
        if state != QMediaPlayer.PlayingState:
            self._save_episode_position()

    def _on_player_error(self, _error, error_string: str) -> None:
        if self._current_episode is not None:
            self._status.showMessage(f"Couldn't play {self._current_episode.title}: {error_string}", 6000)
        elif self._current is not None:
            self._status.showMessage(f"Couldn't play {self._current.title}: {error_string}", 6000)

    def _on_volume_changed(self, volume: int) -> None:
        self._audio_output.setVolume(volume / 100)
        self._settings.set_volume(volume)

    def _on_shuffle_toggled(self, shuffle: bool) -> None:
        self._queue.set_shuffle(shuffle)
        self._settings.set_shuffle(shuffle)
        self._refresh_up_next()

    def _on_repeat_changed(self, repeat: str) -> None:
        self._queue.repeat = repeat
        self._settings.set_repeat(repeat)
        self._refresh_up_next()

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
        self._closing = True
        self._save_episode_position()
        self._podcasts.shutdown()
        self._album_model.shutdown()
        self._player.stop()
        for thread in list(self._background_threads):
            thread.quit()
            thread.wait(BACKGROUND_JOIN_TIMEOUT_MS)
        self._library.close()
        super().closeEvent(event)
