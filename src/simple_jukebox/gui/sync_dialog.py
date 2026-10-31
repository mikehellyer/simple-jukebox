"""File ▸ Sync to Device…: pick a player, choose what goes on it, sync.

Each device is remembered (see DeviceProfile) so a resync is one click.
Working out the plan and copying both run off the GUI thread — over MTP
even checking which songs are already on the player can take a while.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.device_identity import (
    PODCASTS_BESIDE_MUSIC,
    PODCASTS_CUSTOM,
    PODCASTS_IN_MUSIC,
    PODCASTS_SUBFOLDER,
    DeviceCheck,
    check_device,
    default_podcast_location,
    new_device_id,
    podcast_folder,
    read_identity,
    write_identity,
)
from simple_jukebox.core.device_sync import (
    SyncPlan,
    candidate_device_folders,
    free_space,
    plan_file_sync,
    plan_sync,
    read_manifest,
    run_sync,
    tidy_abandoned_sync_folder,
)
from simple_jukebox.core.podcast_download import episode_relative_path
from simple_jukebox.core.library import Library, Track
from simple_jukebox.core.settings import DeviceProfile, SettingsStore

NEW_DEVICE = "New device…"
CONNECTED_SUFFIX = "  — connected"
LOCATION_CHOICES = [
    (PODCASTS_BESIDE_MUSIC, "The device's Podcasts folder (Android players, e.g. NW-A306)"),
    (PODCASTS_IN_MUSIC, "Inside the Music folder (players that only read MUSIC, e.g. NW-A55)"),
    (PODCASTS_CUSTOM, "Another folder…"),
]


def format_bytes(count: int) -> str:
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


def songs(count: int) -> str:
    return f"{count:,} song" + ("" if count == 1 else "s")


def episodes(count: int) -> str:
    return f"{count:,} episode" + ("" if count == 1 else "s")


class _ProgressBridge(QObject):
    """Carries copy progress from the worker thread to the dialog."""

    progress = Signal(int, int, str)


class SyncDialog(QDialog):
    def __init__(
        self, library: Library, settings: SettingsStore, run_in_background: Callable, podcast_store=None, parent=None
    ):
        super().__init__(parent)
        self.setWindowTitle("Sync to Device")
        self.resize(740, 680)
        self._library = library
        self._podcast_store = podcast_store
        self._settings = settings
        self._run_in_background = run_in_background
        self._editing_name: Optional[str] = None  # name of the saved profile being edited
        # Kept with the profile but not shown as fields:
        self._device_id = ""  # matches the identity file on the device
        self._last_podcast_dest = ""
        self._location_auto = True  # podcast location follows the device type until chosen
        self._podcast_custom = ""  # the folder for "Another folder…"
        self._pending_podcast_dest = ""
        self._busy = False
        self._found_existing = 0  # from the last plan, for the "done" message
        self._cancel = threading.Event()
        self._bridge = _ProgressBridge()
        self._bridge.progress.connect(self._on_progress, Qt.QueuedConnection)

        layout = QVBoxLayout(self)

        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox()
        self._device_combo.currentIndexChanged.connect(self._on_device_chosen)
        device_row.addWidget(self._device_combo, stretch=1)
        self._remove_button = QPushButton("Forget Device")
        self._remove_button.clicked.connect(self._forget_device)
        device_row.addWidget(self._remove_button)
        layout.addLayout(device_row)
        self._device_status = QLabel("")
        self._device_status.setWordWrap(True)
        layout.addWidget(self._device_status)

        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Walkman NW-A306")
        form.addRow("Name:", self._name)

        folder_row = QHBoxLayout()
        self._path = QLineEdit()
        self._path.setPlaceholderText("The Music folder on the player or its SD card")
        self._path.textChanged.connect(lambda _text: self._on_music_path_changed())
        folder_row.addWidget(self._path, stretch=1)
        self._detected_button = QToolButton()
        self._detected_button.setText("Detected ▾")
        self._detected_button.setPopupMode(QToolButton.InstantPopup)
        self._detected_menu = QMenu(self)
        self._detected_menu.aboutToShow.connect(self._populate_detected)
        self._detected_button.setMenu(self._detected_menu)
        folder_row.addWidget(self._detected_button)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_row)
        folder_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Music folder:", folder_widget)
        layout.addLayout(form)

        hint = QLabel(
            "Plug the player in by USB (on Linux it appears under “Detected”), "
            "or put its microSD card in a card reader. Songs go into Artist/Album folders inside this folder."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        tabs = QTabWidget()
        music_tab = QWidget()
        music = QVBoxLayout(music_tab)
        self._sync_music = QCheckBox("Sync music to the device")
        self._sync_music.toggled.connect(self._on_mode_changed)
        music.addWidget(self._sync_music)

        self._whole_library = QRadioButton("Entire library")
        self._chosen = QRadioButton("Chosen playlists and artists:")
        group = QButtonGroup(self)
        group.addButton(self._whole_library)
        group.addButton(self._chosen)
        self._whole_library.toggled.connect(self._on_mode_changed)
        music.addWidget(self._whole_library)
        music.addWidget(self._chosen)

        lists_row = QHBoxLayout()
        self._playlist_list = QListWidget()
        self._artist_list = QListWidget()
        for heading, widget in (("Playlists", self._playlist_list), ("Artists", self._artist_list)):
            column = QVBoxLayout()
            label = QLabel(heading)
            label.setStyleSheet("font-weight: 600;")
            column.addWidget(label)
            column.addWidget(widget)
            widget.itemChanged.connect(lambda _item: self._update_summary())
            lists_row.addLayout(column)
        music.addLayout(lists_row, stretch=1)

        self._copy_playlists = QCheckBox("Copy playlists to the device as .m3u files")
        music.addWidget(self._copy_playlists)
        self._remove_unselected = QCheckBox(
            "Remove songs this app copied earlier that are no longer selected"
        )
        self._remove_unselected.setToolTip(
            "Only ever removes files Simple-Jukebox put on the device — never other music or files."
        )
        music.addWidget(self._remove_unselected)
        self._find_existing = QCheckBox(
            "Don't copy songs that are already on the device under a different name"
        )
        self._find_existing.setToolTip(
            "Recognises songs put on the device some other way (by hand, or another app) "
            "by their folder and file name, or failing that their tags — so they aren't copied twice. "
            "Playlists then point at the copy that's already there."
        )
        music.addWidget(self._find_existing)
        tabs.addTab(music_tab, "Music")

        podcasts_tab = QWidget()
        podcasts = QVBoxLayout(podcasts_tab)
        self._sync_podcasts = QCheckBox("Sync podcasts to the device")
        self._sync_podcasts.toggled.connect(self._on_mode_changed)
        podcasts.addWidget(self._sync_podcasts)
        location_row = QHBoxLayout()
        location_row.addWidget(QLabel("Where podcasts go:"))
        self._podcast_location = QComboBox()
        for value, label in LOCATION_CHOICES:
            self._podcast_location.addItem(label, value)
        self._podcast_location.currentIndexChanged.connect(self._on_location_chosen)
        location_row.addWidget(self._podcast_location, stretch=1)
        podcasts.addLayout(location_row)
        podcast_row = QHBoxLayout()
        podcast_row.addWidget(QLabel("Folder:"))
        self._podcast_path = QLineEdit()
        self._podcast_path.setPlaceholderText("Choose the folder on the device")
        self._podcast_path.textChanged.connect(self._update_summary)
        podcast_row.addWidget(self._podcast_path, stretch=1)
        self._podcast_browse = QPushButton("Browse…")
        self._podcast_browse.clicked.connect(self._browse_podcasts)
        podcast_row.addWidget(self._podcast_browse)
        podcasts.addLayout(podcast_row)
        podcast_hint = QLabel(
            "Episodes go into a folder per show, tagged as podcasts. Android players (like the NW-A306) "
            "list anything in their Podcasts folder under Podcasts. Sony's other players (like the NW-A55) "
            "only read the MUSIC folder, so there the show appears as an album, with the genre Podcast."
        )
        podcast_hint.setWordWrap(True)
        podcast_hint.setStyleSheet("color: gray; font-size: 11px;")
        podcasts.addWidget(podcast_hint)
        self._all_podcasts = QRadioButton("All shows")
        self._chosen_podcasts = QRadioButton("Chosen shows:")
        podcast_group = QButtonGroup(self)
        podcast_group.addButton(self._all_podcasts)
        podcast_group.addButton(self._chosen_podcasts)
        self._all_podcasts.toggled.connect(self._on_mode_changed)
        podcasts.addWidget(self._all_podcasts)
        podcasts.addWidget(self._chosen_podcasts)
        self._podcast_list = QListWidget()
        self._podcast_list.itemChanged.connect(lambda _item: self._update_summary())
        podcasts.addWidget(self._podcast_list, stretch=1)
        self._unplayed_only = QCheckBox("Only unplayed episodes")
        self._unplayed_only.setToolTip(
            "Episodes are removed from the device once they're played here (or their download is deleted). "
            "Only episodes Simple-Jukebox copied are ever removed."
        )
        self._unplayed_only.toggled.connect(lambda _on: self._update_summary())
        podcasts.addWidget(self._unplayed_only)
        podcasts.addWidget(QLabel(
            "Only downloaded episodes are synced. Episodes copied earlier that are played or deleted here "
            "are removed from the device."
        ))
        tabs.addTab(podcasts_tab, "Podcasts")
        layout.addWidget(tabs, stretch=1)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._progress = QProgressBar()
        self._progress.hide()
        layout.addWidget(self._progress)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: gray;")
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._sync_button = QPushButton("Sync Now")
        self._sync_button.setDefault(True)
        self._sync_button.clicked.connect(self._start_sync)
        buttons.addWidget(self._sync_button)
        self._cancel_button = QPushButton("Cancel Sync")
        self._cancel_button.clicked.connect(self._cancel.set)
        self._cancel_button.hide()
        buttons.addWidget(self._cancel_button)
        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.reject)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

        self._populate_lists()
        self._reload_devices(select=self._connected_profile_name())

    # --- devices --------------------------------------------------------

    def _reload_devices(self, select: Optional[str] = None) -> None:
        devices = self._settings.devices
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for device in devices:
            connected = device.device_id and check_device(Path(device.path), device.device_id).status == DeviceCheck.MATCH
            self._device_combo.addItem(device.name + (CONNECTED_SUFFIX if connected else ""), device.name)
        self._device_combo.addItem(NEW_DEVICE, None)
        index = self._device_combo.findData(select) if select else 0
        self._device_combo.setCurrentIndex(max(0, index))
        self._device_combo.blockSignals(False)
        self._on_device_chosen()

    def _connected_profile_name(self) -> Optional[str]:
        """Which saved device is plugged in — found by its identity file,
        at its usual folder or (if it now mounts somewhere else, as MTP
        devices sometimes do) at any folder that looks like a player."""
        devices = [d for d in self._settings.devices if d.device_id]
        for device in devices:
            if check_device(Path(device.path), device.device_id).status == DeviceCheck.MATCH:
                return device.name
        for folder in candidate_device_folders():
            identity = read_identity(folder)
            device = next((d for d in devices if identity and d.device_id == identity["id"]), None)
            if device is not None:
                device.path = str(folder)  # follow it to its new mount point
                self._settings.save_device(device)
                return device.name
        return None

    def _profile_named(self, name: str) -> Optional[DeviceProfile]:
        return next((d for d in self._settings.devices if d.name == name), None)

    def _profile_with_id(self, device_id: str) -> Optional[DeviceProfile]:
        return next((d for d in self._settings.devices if device_id and d.device_id == device_id), None)

    def _on_device_chosen(self, *_args) -> None:
        name = self._device_combo.currentData()
        profile = self._profile_named(name) if name else None
        self._editing_name = profile.name if profile else None
        self._remove_button.setEnabled(profile is not None)
        profile = profile or DeviceProfile(name="", path="", whole_library=False)
        self._name.setText(profile.name)
        self._path.setText(profile.path)
        (self._whole_library if profile.whole_library else self._chosen).setChecked(True)
        self._set_checked(self._playlist_list, set(profile.playlist_ids))
        self._set_checked(self._artist_list, set(profile.artists))
        self._copy_playlists.setChecked(profile.copy_playlists)
        self._remove_unselected.setChecked(profile.remove_unselected)
        self._find_existing.setChecked(profile.find_existing)
        self._sync_music.setChecked(profile.sync_music)
        self._sync_podcasts.setChecked(profile.sync_podcasts)
        self._device_id = profile.device_id
        self._last_podcast_dest = profile.last_podcast_dest
        self._podcast_custom = profile.podcast_path
        self._location_auto = not profile.podcast_location
        self._set_location(profile.podcast_location or default_podcast_location(profile.path))
        (self._all_podcasts if profile.all_podcasts else self._chosen_podcasts).setChecked(True)
        self._set_checked(self._podcast_list, set(profile.podcast_ids))
        self._unplayed_only.setChecked(profile.podcast_unplayed_only)
        self._on_mode_changed()
        self._status.setText("")

    def _current_profile(self) -> DeviceProfile:
        return DeviceProfile(
            name=self._name.text().strip(),
            path=self._path.text().strip(),
            whole_library=self._whole_library.isChecked(),
            playlist_ids=self._checked(self._playlist_list),
            artists=self._checked(self._artist_list),
            copy_playlists=self._copy_playlists.isChecked(),
            remove_unselected=self._remove_unselected.isChecked(),
            find_existing=self._find_existing.isChecked(),
            sync_music=self._sync_music.isChecked(),
            sync_podcasts=self._sync_podcasts.isChecked(),
            podcast_path=self._podcast_custom,
            podcast_location="" if self._location_auto else self._location(),
            last_podcast_dest=self._last_podcast_dest,
            device_id=self._device_id,
            all_podcasts=self._all_podcasts.isChecked(),
            podcast_ids=self._checked(self._podcast_list),
            podcast_unplayed_only=self._unplayed_only.isChecked(),
        )

    def _save_profile(self) -> bool:
        profile = self._current_profile()
        if not profile.name or not profile.path:
            return False
        self._settings.save_device(profile, old_name=self._editing_name)
        self._editing_name = profile.name
        self._reload_devices(select=profile.name)
        return True

    def _forget_device(self) -> None:
        if self._editing_name is None:
            return
        answer = QMessageBox.question(
            self,
            "Forget Device",
            f"Forget “{self._editing_name}”?\nNothing on the device itself is changed.",
        )
        if answer == QMessageBox.Yes:
            self._settings.remove_device(self._editing_name)
            self._reload_devices()

    def _populate_detected(self) -> None:
        self._detected_menu.clear()
        folders = candidate_device_folders()
        if not folders:
            action = self._detected_menu.addAction("No players or cards found — is it plugged in and unlocked?")
            action.setEnabled(False)
            return
        for folder in folders:
            self._detected_menu.addAction(str(folder), lambda f=folder: self._path.setText(str(f)))

    def _browse_podcasts(self) -> None:
        start = self._podcast_folder() or self._path.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Podcasts Folder on the Device", start)
        if folder:
            self._podcast_custom = folder
            self._show_podcast_folder()

    # --- where podcasts go ----------------------------------------------

    def _location(self) -> str:
        return self._podcast_location.currentData() or PODCASTS_IN_MUSIC

    def _set_location(self, location: str) -> None:
        self._podcast_location.blockSignals(True)
        self._podcast_location.setCurrentIndex(max(0, self._podcast_location.findData(location)))
        self._podcast_location.blockSignals(False)
        self._show_podcast_folder()

    def _on_location_chosen(self, *_args) -> None:
        self._location_auto = False
        self._show_podcast_folder()

    def _podcast_folder(self) -> str:
        return podcast_folder(self._path.text().strip(), self._location(), self._podcast_custom)

    def _show_podcast_folder(self) -> None:
        """The folder box shows where episodes will go; it's only editable
        (by Browse…) for "Another folder"."""
        custom = self._location() == PODCASTS_CUSTOM
        self._podcast_path.blockSignals(True)
        self._podcast_path.setText(self._podcast_folder())
        self._podcast_path.blockSignals(False)
        self._podcast_path.setReadOnly(True)
        self._podcast_browse.setVisible(custom)
        self._update_summary()

    def _on_music_path_changed(self) -> None:
        # Until a location is chosen by hand, follow what kind of player it is.
        if self._location_auto:
            self._set_location(default_podcast_location(self._path.text().strip()))
        else:
            self._show_podcast_folder()

    def _browse(self) -> None:
        start = self._path.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Folder on the Device", start)
        if folder:
            self._path.setText(folder)

    # --- what to sync ---------------------------------------------------

    def _populate_lists(self) -> None:
        self._playlist_list.blockSignals(True)
        self._artist_list.blockSignals(True)
        for playlist in self._library.playlists():
            item = QListWidgetItem(f"{playlist.name}  ({playlist.track_count})")
            item.setData(Qt.UserRole, playlist.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._playlist_list.addItem(item)
        for artist in self._library.artists():
            item = QListWidgetItem(artist)
            item.setData(Qt.UserRole, artist)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._artist_list.addItem(item)
        self._playlist_list.blockSignals(False)
        self._artist_list.blockSignals(False)
        self._podcast_list.blockSignals(True)
        for podcast in self._podcast_store.podcasts() if self._podcast_store else []:
            item = QListWidgetItem(f"{podcast.title}  ({podcast.downloaded_count} downloaded)")
            item.setData(Qt.UserRole, podcast.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._podcast_list.addItem(item)
        self._podcast_list.blockSignals(False)

    @staticmethod
    def _set_checked(widget: QListWidget, values: set) -> None:
        widget.blockSignals(True)
        for row in range(widget.count()):
            item = widget.item(row)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in values else Qt.Unchecked)
        widget.blockSignals(False)

    @staticmethod
    def _checked(widget: QListWidget) -> list:
        return [
            widget.item(row).data(Qt.UserRole)
            for row in range(widget.count())
            if widget.item(row).checkState() == Qt.Checked
        ]

    def _on_mode_changed(self, *_args) -> None:
        music = self._sync_music.isChecked()
        for widget in (self._whole_library, self._chosen, self._copy_playlists, self._remove_unselected,
                       self._find_existing):
            widget.setEnabled(music)
        chosen = music and self._chosen.isChecked()
        self._playlist_list.setEnabled(chosen)
        self._artist_list.setEnabled(chosen)
        podcasts = self._sync_podcasts.isChecked()
        for widget in (self._podcast_location, self._podcast_path, self._podcast_browse, self._all_podcasts,
                       self._chosen_podcasts, self._unplayed_only):
            widget.setEnabled(podcasts)
        self._podcast_list.setEnabled(podcasts and self._chosen_podcasts.isChecked())
        self._update_summary()

    def _podcast_items(self) -> list[tuple[str, str]]:
        """(downloaded file, path on the device) for each episode to sync."""
        if self._podcast_store is None or not self._sync_podcasts.isChecked():
            return []
        ids = None if self._all_podcasts.isChecked() else self._checked(self._podcast_list)
        chosen = self._podcast_store.downloaded_episodes(ids, unplayed_only=self._unplayed_only.isChecked())
        return [
            (episode.local_path, episode_relative_path(episode.podcast_title, episode))
            for episode in chosen
            if episode.downloaded
        ]

    def _selection(self) -> tuple[list[Track], dict[str, list[Track]]]:
        """(songs to put on the device, playlists to write as .m3u)."""
        playlists = {p.id: p.name for p in self._library.playlists()}
        if self._whole_library.isChecked():
            tracks = self._library.tracks()
            playlist_ids = list(playlists)
        else:
            playlist_ids = self._checked(self._playlist_list)
            tracks = []
            for playlist_id in playlist_ids:
                tracks.extend(self._library.playlist_tracks(playlist_id))
            for artist in self._checked(self._artist_list):
                tracks.extend(self._library.tracks(artist=artist))
        m3u = {}
        if self._copy_playlists.isChecked():
            m3u = {playlists[i]: self._library.playlist_tracks(i) for i in playlist_ids if i in playlists}
        return tracks, m3u

    def _update_summary(self, *_args) -> None:
        tracks, _ = self._selection() if self._sync_music.isChecked() else ([], {})
        unique = {t.path for t in tracks}
        items = self._podcast_items()
        size = 0
        for path in unique | {source for source, _ in items}:
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
        parts = []
        if self._sync_music.isChecked():
            parts.append(songs(len(unique)))
        if self._sync_podcasts.isChecked():
            parts.append(episodes(len(items)))
        text = f"Selected: {' and '.join(parts) or 'nothing'}, {format_bytes(size)}."
        path = self._path.text().strip()
        if path and Path(path).is_dir():
            free = free_space(Path(path))
            if free is not None:
                text += f"  {format_bytes(free)} free on the device."
        elif path:
            text += "  That folder can't be found — is the device plugged in?"
        self._summary.setText(text)
        self._update_device_status()

    def _update_device_status(self) -> None:
        if not hasattr(self, "_device_status"):
            return
        path, name = self._path.text().strip(), self._name.text().strip() or "this device"
        if not path:
            self._device_status.setText("")
            return
        result = check_device(Path(path), self._device_id)
        text, colour = {
            DeviceCheck.MATCH: (f"✓ Connected — this is “{name}”.", "#2e8b57"),
            DeviceCheck.NEW: (f"New device — it will be marked as “{name}” on its first sync.", "gray"),
            DeviceCheck.OTHER: (
                f"⚠ A different device is connected here: “{result.found_name or 'another device'}”. "
                f"Plug in “{name}” to sync it.", "#c0392b"),
            DeviceCheck.UNMARKED: (
                f"⚠ The device here isn't marked as “{name}” — is the right one plugged in?", "#d35400"),
            DeviceCheck.NOT_FOUND: (f"“{name}” isn't connected.", "gray"),
        }[result.status]
        self._device_status.setText(text)
        self._device_status.setStyleSheet(f"color: {colour};")

    # --- syncing --------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self._device_combo, self._remove_button, self._name, self._path, self._detected_button,
            self._whole_library, self._chosen, self._copy_playlists, self._remove_unselected,
            self._find_existing, self._sync_music, self._sync_podcasts, self._podcast_path,
            self._podcast_location, self._podcast_browse,
            self._all_podcasts, self._chosen_podcasts, self._unplayed_only, self._podcast_list,
            self._playlist_list, self._artist_list,
            self._sync_button, self._close_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._on_mode_changed()
        self._cancel_button.setVisible(busy)
        self._progress.setVisible(busy)

    def _start_sync(self) -> None:
        profile = self._current_profile()
        if not profile.name:
            QMessageBox.information(self, "Sync to Device", "Give the device a name first.")
            return
        if not profile.sync_music and not profile.sync_podcasts:
            QMessageBox.information(self, "Sync to Device", "Tick “Sync music” or “Sync podcasts” first.")
            return
        not_found = (
            "can't be found. Make sure the player is plugged in and unlocked "
            "(or its card is in the reader)."
        )
        dest = Path(profile.path) if profile.path else None
        if dest is None or not dest.is_dir():
            # Needed even for podcasts only: it's where the device's identity lives.
            QMessageBox.warning(self, "Sync to Device", f"The Music folder {not_found}")
            return
        if not self._confirm_right_device(profile, dest):
            return
        podcast_dest = None
        if profile.sync_podcasts:
            podcast_dest = Path(self._podcast_folder()) if self._podcast_folder() else None
            if podcast_dest is not None and not podcast_dest.is_dir() and podcast_dest.parent.is_dir():
                try:
                    podcast_dest.mkdir()  # first sync: the device has no Podcasts folder yet
                except OSError:
                    pass
            if podcast_dest is None or not podcast_dest.is_dir():
                QMessageBox.warning(self, "Sync to Device", f"The Podcasts folder {not_found}")
                return
        self._save_profile()
        old_podcast_folders = self._old_podcast_folders(profile, podcast_dest) if profile.sync_podcasts else []
        skip = [PODCASTS_SUBFOLDER] if podcast_dest is not None and podcast_dest.parent == dest else []
        tracks, playlists = self._selection() if profile.sync_music else ([], {})
        items = self._podcast_items()
        if profile.sync_music and not tracks and not profile.remove_unselected and not items:
            QMessageBox.information(self, "Sync to Device", "Nothing is selected to sync.")
            return

        self._cancel.clear()
        self._set_busy(True)
        self._progress.setRange(0, 0)  # busy, until we know the total
        self._status.setText("Checking what's already on the device…")
        remove, find_existing = profile.remove_unselected, profile.find_existing
        cache_path = self._tag_cache_path(dest) if dest else None
        sync_music = profile.sync_music
        bridge = self._bridge
        self._run_in_background(
            lambda: (
                plan_sync(
                    dest, tracks, playlists,
                    remove_unselected=remove, find_existing=find_existing,
                    progress=bridge.progress.emit, tag_cache_path=cache_path, skip_folders=skip,
                ) if sync_music else None,
                plan_file_sync(podcast_dest, items) if podcast_dest is not None else None,
                [plan_file_sync(folder, []) for folder in old_podcast_folders],
            ),
            self._on_planned,
        )
        self._pending_podcast_dest = str(podcast_dest) if podcast_dest is not None else self._last_podcast_dest

    def _confirm_right_device(self, profile: DeviceProfile, dest: Path) -> bool:
        """Refuse to sync onto the wrong player. Two Walkmans can mount at
        the same place, so the identity file decides, not the folder."""
        result = check_device(dest, profile.device_id)
        if result.status in (DeviceCheck.MATCH, DeviceCheck.NEW):
            return True
        if result.status == DeviceCheck.OTHER:
            other = self._profile_with_id(result.found_id)
            if other is None and not profile.device_id:
                # Marked by a device profile that has since been forgotten.
                answer = QMessageBox.question(
                    self,
                    "Use This Device?",
                    f"This device was set up as “{result.found_name or 'another device'}”, which isn't one of "
                    f"your saved devices any more.\n\nUse it as “{profile.name}”?",
                )
                if answer == QMessageBox.Yes:
                    self._device_id = result.found_id
                    return True
                return False
            found = other.name if other is not None else (result.found_name or "another device")
            QMessageBox.warning(
                self,
                "Wrong Device",
                f"The device connected here is “{found}”, not “{profile.name}”.\n\n"
                f"Nothing was synced. Plug in “{profile.name}”"
                + (f", or choose “{found}” above to sync that one." if other is not None else "."),
            )
            if other is not None:
                self._reload_devices(select=other.name)
            return False
        # UNMARKED: a known device, but this folder has no identity.
        answer = QMessageBox.question(
            self,
            "Is This the Right Device?",
            f"The device at this folder isn't marked as “{profile.name}” — it has never been synced as that "
            "device. If you've reset the player or swapped its card, it's fine to carry on.\n\n"
            f"Is this “{profile.name}”?",
        )
        return answer == QMessageBox.Yes

    def _old_podcast_folders(self, profile: DeviceProfile, podcast_dest: Optional[Path]) -> list[Path]:
        """Where episodes were synced before, if podcasts now go elsewhere
        — so the copies left there can be cleared away."""
        music = profile.path
        candidates = {profile.last_podcast_dest} | {
            podcast_folder(music, location) for location in (PODCASTS_BESIDE_MUSIC, PODCASTS_IN_MUSIC)
        }
        current = str(podcast_dest) if podcast_dest is not None else ""
        return [
            Path(folder) for folder in sorted(c for c in candidates if c and c != current)
            if Path(folder).is_dir() and read_manifest(Path(folder))
        ]

    @staticmethod
    def _tag_cache_path(dest: Path) -> Path:
        """Tags of songs already on a device, cached on this computer so
        resyncs don't read them over USB again — one file per device folder."""
        key = hashlib.sha1(str(dest).encode("utf-8")).hexdigest()[:16]
        return Path(user_cache_dir("Simple-Jukebox")) / "device-tags" / f"{key}.json"

    def _on_planned(self, plans) -> None:
        music_plan, podcast_plan, old_podcast_plans = plans
        old_podcast_plans = [p for p in old_podcast_plans if p.deletes]
        active = [plan for plan in (music_plan, podcast_plan) if plan is not None] + old_podcast_plans
        free = next((f for f in (free_space(p.dest) for p in active) if f is not None), None)
        needed = sum(p.bytes_to_copy - p.bytes_to_free for p in active)
        if free is not None and needed > free:
            self._set_busy(False)
            self._status.setText("")
            QMessageBox.warning(
                self,
                "Not Enough Space",
                f"This needs {format_bytes(needed)} more space, but the device only has "
                f"{format_bytes(free)} free. Choose fewer playlists, artists or shows.",
            )
            return
        nothing_to_do = all(not p.copies and not p.all_deletes and not p.playlists for p in active)
        if nothing_to_do:
            self._set_busy(False)
            self._mark_synced()
            parts = []
            if music_plan is not None:
                parts.append(songs(music_plan.song_count))
            if podcast_plan is not None:
                parts.append(episodes(podcast_plan.unchanged))
            self._status.setText(f"Already up to date — {' and '.join(parts)} on the device.")
            return
        reasons = []
        if music_plan is not None and music_plan.deletes:
            reasons.append(f"• {songs(len(music_plan.deletes))} copied by an earlier sync no longer selected.")
        if music_plan is not None and music_plan.duplicate_deletes:
            reasons.append(
                f"• {songs(len(music_plan.duplicate_deletes))} copied by an earlier sync that duplicate "
                "songs you already had on the device — your copies are kept."
            )
        if podcast_plan is not None and podcast_plan.deletes:
            reasons.append(
                f"• {episodes(len(podcast_plan.deletes))} copied earlier — since played, deleted "
                "or no longer selected."
            )
        for old in old_podcast_plans:
            reasons.append(
                f"• {episodes(len(old.deletes))} copied earlier to {old.dest}, where podcasts no longer go."
            )
        if reasons:
            freed = sum(p.bytes_to_free for p in active)
            answer = QMessageBox.question(
                self,
                "Remove from Device?",
                "\n".join(reasons)
                + f"\n\nThese will be removed from the device ({format_bytes(freed)}). "
                "Only files Simple-Jukebox copied are ever removed.\n\nContinue?",
            )
            if answer != QMessageBox.Yes:
                self._set_busy(False)
                self._status.setText("Sync cancelled.")
                return

        self._mark_synced()  # the device is about to be written to: make sure it carries its identity
        copying = []
        if music_plan is not None and music_plan.copies:
            copying.append(songs(len(music_plan.copies)))
        if podcast_plan is not None and podcast_plan.copies:
            copying.append(episodes(len(podcast_plan.copies)))
        self._status.setText(
            f"Copying {' and '.join(copying) or 'nothing new'} ({format_bytes(sum(p.bytes_to_copy for p in active))})…"
        )
        self._found_existing = music_plan.found_existing if music_plan is not None else 0
        bridge, cancel = self._bridge, self._cancel

        def work():
            results = tuple(
                run_sync(plan, progress=bridge.progress.emit, cancelled=cancel.is_set) if plan is not None else None
                for plan in (music_plan, podcast_plan)
            )
            cleared = [run_sync(plan, cancelled=cancel.is_set) for plan in old_podcast_plans]
            for plan in old_podcast_plans:
                tidy_abandoned_sync_folder(plan.dest)
            return results + (cleared,)

        self._run_in_background(work, self._on_synced)

    def _mark_synced(self) -> None:
        """Write the device's identity (first sync) and remember where its
        podcasts now go."""
        profile = self._current_profile()
        if not self._device_id:
            self._device_id = new_device_id()
        identity = read_identity(Path(profile.path))
        if identity is None or identity.get("id") != self._device_id or identity.get("name") != profile.name:
            try:
                write_identity(Path(profile.path), self._device_id, profile.name)
            except OSError:
                pass  # a read-only card still syncs; it just can't be recognised later
        self._last_podcast_dest = self._pending_podcast_dest or self._last_podcast_dest
        self._location_auto = False  # settled now: keep it even if the device type is guessed differently later
        # Save without reloading the device list — that would reset the
        # window mid-sync.
        profile = self._current_profile()
        if profile.name and profile.path:
            self._settings.save_device(profile, old_name=self._editing_name)
            self._editing_name = profile.name
        self._update_device_status()

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        if label:
            self._progress.setFormat(f"%v / %m — {label}")

    def _on_synced(self, results) -> None:
        self._set_busy(False)
        music, podcasts, cleared = results
        done = [r for r in (music, podcasts) if r is not None] + list(cleared)
        copied = []
        if music is not None and music.copied:
            copied.append(songs(music.copied))
        if podcasts is not None and podcasts.copied:
            copied.append(episodes(podcasts.copied))
        copied_text = " and ".join(copied) or "nothing"
        deleted = sum(r.deleted for r in done)
        if any(r.cancelled for r in done):
            text = f"Sync cancelled after copying {copied_text}. Sync again to finish."
        else:
            text = f"Done — copied {copied_text}" if copied else "Done — nothing new to copy"
            if deleted:
                text += f", removed {deleted:,} from the device"
            text += "."
            if self._found_existing:
                text += (
                    f" {songs(self._found_existing)} already on the device under other names "
                    "weren't copied again."
                )
        failed = [f for r in done for f in r.failed]
        if failed:
            first, error = failed[0]
            text += f"\n{len(failed):,} couldn't be copied (first: {first} — {error})."
        self._status.setText(text)
        self._update_summary()

    def reject(self) -> None:
        # Close button, window X and Escape all end up here.
        if self._busy:
            return  # cancel the sync first
        self._save_profile()
        super().reject()
