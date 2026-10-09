"""The left-hand sidebar: library views ("All Music", …) and the user's
playlists. Tracks dragged from the table can be dropped on a playlist."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

from simple_jukebox.core.library import Playlist
from simple_jukebox.gui.track_model import TRACKS_MIME, decode_tracks

KIND_ROLE = Qt.UserRole
VALUE_ROLE = Qt.UserRole + 1
KIND_LIBRARY = "library"
KIND_PLAYLIST = "playlist"


class SourceList(QListWidget):
    tracks_dropped_on_playlist = Signal(int, list)  # playlist id, track ids

    def __init__(self, library_views: list[str], parent=None):
        super().__init__(parent)
        self._library_views = library_views
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDropIndicatorShown(True)
        self.set_playlists([])

    def _add_heading(self, text: str) -> None:
        item = QListWidgetItem(text.upper())
        item.setFlags(Qt.NoItemFlags)
        font = QFont()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        item.setFont(font)
        self.addItem(item)

    def set_playlists(self, playlists: list[Playlist]) -> None:
        """Rebuild the list, keeping whatever was selected if it still exists."""
        selected = self.current_source()
        self.blockSignals(True)
        self.clear()
        self._add_heading("Library")
        for name in self._library_views:
            item = QListWidgetItem(name)
            item.setData(KIND_ROLE, KIND_LIBRARY)
            item.setData(VALUE_ROLE, name)
            self.addItem(item)
        self._add_heading("Playlists")
        for playlist in playlists:
            item = QListWidgetItem(f"♫  {playlist.name}")
            item.setData(KIND_ROLE, KIND_PLAYLIST)
            item.setData(VALUE_ROLE, playlist.id)
            item.setToolTip(f"{playlist.track_count} songs")
            self.addItem(item)
        self.blockSignals(False)
        restored = selected is not None and self.select_source(*selected)
        if not restored:
            self.select_source(KIND_LIBRARY, self._library_views[0])

    def select_source(self, kind: str, value) -> bool:
        for row in range(self.count()):
            item = self.item(row)
            if item.data(KIND_ROLE) == kind and item.data(VALUE_ROLE) == value:
                self.setCurrentRow(row)
                return True
        return False

    def current_source(self):
        """(kind, value) of the selected entry, or None."""
        item = self.currentItem()
        if item is None or item.data(KIND_ROLE) is None:
            return None
        return item.data(KIND_ROLE), item.data(VALUE_ROLE)

    def playlist_at(self, pos):
        item = self.itemAt(pos)
        if item is not None and item.data(KIND_ROLE) == KIND_PLAYLIST:
            return item.data(VALUE_ROLE)
        return None

    # --- drops ----------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(TRACKS_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self.playlist_at(event.position().toPoint()) is not None:
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        playlist_id = self.playlist_at(event.position().toPoint())
        payload = decode_tracks(event.mimeData())
        if playlist_id is None or payload is None:
            event.ignore()
            return
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.tracks_dropped_on_playlist.emit(playlist_id, list(payload["ids"]))
