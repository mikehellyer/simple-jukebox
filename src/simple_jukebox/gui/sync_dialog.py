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

from simple_jukebox.core.device_sync import (
    SyncPlan,
    candidate_device_folders,
    default_podcast_folder_for,
    free_space,
    plan_file_sync,
    plan_sync,
    run_sync,
)
from simple_jukebox.core.podcast_download import episode_relative_path
from simple_jukebox.core.library import Library, Track
from simple_jukebox.core.settings import DeviceProfile, SettingsStore

NEW_DEVICE = "New device…"


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

        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Walkman NW-A306")
        form.addRow("Name:", self._name)

        folder_row = QHBoxLayout()
        self._path = QLineEdit()
        self._path.setPlaceholderText("The Music folder on the player or its SD card")
        self._path.textChanged.connect(self._update_summary)
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
        podcast_row = QHBoxLayout()
        podcast_row.addWidget(QLabel("Podcasts folder:"))
        self._podcast_path = QLineEdit()
        self._podcast_path.setPlaceholderText("The Podcasts folder beside the device's Music folder")
        self._podcast_path.textChanged.connect(self._update_summary)
        podcast_row.addWidget(self._podcast_path, stretch=1)
        podcast_browse = QPushButton("Browse…")
        podcast_browse.clicked.connect(self._browse_podcasts)
        podcast_row.addWidget(podcast_browse)
        podcasts.addLayout(podcast_row)
        podcast_hint = QLabel(
            "Episodes go into a folder per show, tagged as podcasts. On an Android player such as a "
            "Walkman, anything in its Podcasts folder is listed under Podcasts rather than Music."
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
        self._reload_devices()

    # --- devices --------------------------------------------------------

    def _reload_devices(self, select: Optional[str] = None) -> None:
        devices = self._settings.devices
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for device in devices:
            self._device_combo.addItem(device.name)
        self._device_combo.addItem(NEW_DEVICE)
        index = self._device_combo.findText(select) if select else 0
        self._device_combo.setCurrentIndex(max(0, index))
        self._device_combo.blockSignals(False)
        self._on_device_chosen()

    def _profile_named(self, name: str) -> Optional[DeviceProfile]:
        return next((d for d in self._settings.devices if d.name == name), None)

    def _on_device_chosen(self, *_args) -> None:
        name = self._device_combo.currentText()
        profile = self._profile_named(name) if name != NEW_DEVICE else None
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
        self._podcast_path.setText(profile.podcast_path)
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
            podcast_path=self._podcast_path.text().strip(),
            all_podcasts=self._all_podcasts.isChecked(),
            podcast_ids=self._checked(self._podcast_list),
            podcast_unplayed_only=self._unplayed_only.isChecked(),
        )

    def _save_profile(self) -> bool:
        profile = self._current_profile()
        if not profile.name or not (profile.path or profile.podcast_path):
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
            self._podcast_path.setText(folder)

    def _podcast_folder(self) -> str:
        """The chosen Podcasts folder, or the default one beside Music."""
        return self._podcast_path.text().strip() or default_podcast_folder_for(self._path.text().strip())

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
        for widget in (self._podcast_path, self._all_podcasts, self._chosen_podcasts, self._unplayed_only):
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

    # --- syncing --------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self._device_combo, self._remove_button, self._name, self._path, self._detected_button,
            self._whole_library, self._chosen, self._copy_playlists, self._remove_unselected,
            self._find_existing, self._sync_music, self._sync_podcasts, self._podcast_path,
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
        if profile.sync_music and (dest is None or not dest.is_dir()):
            QMessageBox.warning(self, "Sync to Device", f"The Music folder {not_found}")
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
                    progress=bridge.progress.emit, tag_cache_path=cache_path,
                ) if sync_music else None,
                plan_file_sync(podcast_dest, items) if podcast_dest is not None else None,
            ),
            self._on_planned,
        )

    @staticmethod
    def _tag_cache_path(dest: Path) -> Path:
        """Tags of songs already on a device, cached on this computer so
        resyncs don't read them over USB again — one file per device folder."""
        key = hashlib.sha1(str(dest).encode("utf-8")).hexdigest()[:16]
        return Path(user_cache_dir("Simple-Jukebox")) / "device-tags" / f"{key}.json"

    def _on_planned(self, plans) -> None:
        music_plan, podcast_plan = plans
        active = [plan for plan in plans if plan is not None]
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
        self._run_in_background(
            lambda: tuple(
                run_sync(plan, progress=bridge.progress.emit, cancelled=cancel.is_set) if plan is not None else None
                for plan in (music_plan, podcast_plan)
            ),
            self._on_synced,
        )

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        if label:
            self._progress.setFormat(f"%v / %m — {label}")

    def _on_synced(self, results) -> None:
        self._set_busy(False)
        music, podcasts = results
        done = [r for r in results if r is not None]
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
