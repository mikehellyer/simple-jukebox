"""File ▸ Sync to Device…: pick a player, choose what goes on it, sync.

Each device is remembered (see DeviceProfile) so a resync is one click.
Working out the plan and copying both run off the GUI thread — over MTP
even checking which songs are already on the player can take a while.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Optional

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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.device_sync import (
    SyncPlan,
    candidate_device_folders,
    free_space,
    plan_sync,
    run_sync,
)
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


class _ProgressBridge(QObject):
    """Carries copy progress from the worker thread to the dialog."""

    progress = Signal(int, int, str)


class SyncDialog(QDialog):
    def __init__(self, library: Library, settings: SettingsStore, run_in_background: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sync to Device")
        self.resize(720, 620)
        self._library = library
        self._settings = settings
        self._run_in_background = run_in_background
        self._editing_name: Optional[str] = None  # name of the saved profile being edited
        self._busy = False
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
        form.addRow("Folder:", folder_widget)
        layout.addLayout(form)

        hint = QLabel(
            "Plug the player in by USB (on Linux it appears under “Detected”), "
            "or put its microSD card in a card reader. Songs go into Artist/Album folders inside this folder."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        self._whole_library = QRadioButton("Entire library")
        self._chosen = QRadioButton("Chosen playlists and artists:")
        group = QButtonGroup(self)
        group.addButton(self._whole_library)
        group.addButton(self._chosen)
        self._whole_library.toggled.connect(self._on_mode_changed)
        layout.addWidget(self._whole_library)
        layout.addWidget(self._chosen)

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
        layout.addLayout(lists_row, stretch=1)

        self._copy_playlists = QCheckBox("Copy playlists to the device as .m3u files")
        layout.addWidget(self._copy_playlists)
        self._remove_unselected = QCheckBox(
            "Remove songs this app copied earlier that are no longer selected"
        )
        self._remove_unselected.setToolTip(
            "Only ever removes files Simple-Jukebox put on the device — never other music or files."
        )
        layout.addWidget(self._remove_unselected)

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
        chosen = self._chosen.isChecked()
        self._playlist_list.setEnabled(chosen)
        self._artist_list.setEnabled(chosen)
        self._update_summary()

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
        tracks, _ = self._selection()
        unique = {t.path: t for t in tracks}
        size = 0
        for path in unique:
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
        text = f"Selected: {len(unique):,} songs, {format_bytes(size)}."
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
            self._sync_button, self._close_button,
        ):
            widget.setEnabled(not busy)
        chosen = self._chosen.isChecked() and not busy
        self._playlist_list.setEnabled(chosen)
        self._artist_list.setEnabled(chosen)
        self._cancel_button.setVisible(busy)
        self._progress.setVisible(busy)

    def _start_sync(self) -> None:
        profile = self._current_profile()
        if not profile.name:
            QMessageBox.information(self, "Sync to Device", "Give the device a name first.")
            return
        dest = Path(profile.path)
        if not profile.path or not dest.is_dir():
            QMessageBox.warning(
                self,
                "Sync to Device",
                "That folder can't be found. Make sure the player is plugged in and unlocked "
                "(or its card is in the reader), then pick its Music folder.",
            )
            return
        self._save_profile()
        tracks, playlists = self._selection()
        if not tracks and not profile.remove_unselected:
            QMessageBox.information(self, "Sync to Device", "Nothing is selected to sync.")
            return

        self._cancel.clear()
        self._set_busy(True)
        self._progress.setRange(0, 0)  # busy, until we know the total
        self._status.setText("Checking what's already on the device…")
        remove = profile.remove_unselected
        self._run_in_background(
            lambda: plan_sync(dest, tracks, playlists, remove_unselected=remove),
            self._on_planned,
        )

    def _on_planned(self, plan: SyncPlan) -> None:
        free = free_space(plan.dest)
        needed = plan.bytes_to_copy - plan.bytes_to_free
        if free is not None and needed > free:
            self._set_busy(False)
            self._status.setText("")
            QMessageBox.warning(
                self,
                "Not Enough Space",
                f"This needs {format_bytes(needed)} more space, but the device only has "
                f"{format_bytes(free)} free. Choose fewer playlists or artists.",
            )
            return
        if not plan.copies and not plan.deletes and not plan.playlists:
            self._set_busy(False)
            self._status.setText(f"Already up to date — {plan.unchanged:,} songs on the device.")
            return
        if plan.deletes:
            answer = QMessageBox.question(
                self,
                "Remove Songs?",
                f"{len(plan.deletes):,} songs copied by an earlier sync are no longer selected "
                f"and will be removed from the device ({format_bytes(plan.bytes_to_free)}).\n\n"
                "Continue?",
            )
            if answer != QMessageBox.Yes:
                self._set_busy(False)
                self._status.setText("Sync cancelled.")
                return

        self._status.setText(
            f"Copying {len(plan.copies):,} songs ({format_bytes(plan.bytes_to_copy)})"
            + (f", removing {len(plan.deletes):,}" if plan.deletes else "")
            + "…"
        )
        bridge, cancel = self._bridge, self._cancel
        self._run_in_background(
            lambda: run_sync(plan, progress=bridge.progress.emit, cancelled=cancel.is_set),
            self._on_synced,
        )

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        if label:
            self._progress.setFormat(f"%v / %m — {label}")

    def _on_synced(self, result) -> None:
        self._set_busy(False)
        if result.cancelled:
            text = f"Sync cancelled after copying {result.copied:,} songs. Sync again to finish."
        else:
            text = f"Done — copied {result.copied:,} songs"
            if result.deleted:
                text += f", removed {result.deleted:,}"
            text += "."
        if result.failed:
            first, error = result.failed[0]
            text += f"\n{len(result.failed):,} couldn't be copied (first: {first} — {error})."
        self._status.setText(text)
        self._update_summary()

    def reject(self) -> None:
        # Close button, window X and Escape all end up here.
        if self._busy:
            return  # cancel the sync first
        self._save_profile()
        super().reject()
