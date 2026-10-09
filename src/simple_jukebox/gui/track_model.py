"""Table model behind the main track list."""
from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtGui import QFont

from simple_jukebox.core.library import Track
from simple_jukebox.core.text import format_duration

COLUMNS = ("", "#", "Title", "Artist", "Album", "Year", "Genre", "Time", "Plays", "Rating")
NOW_PLAYING_COLUMN = 0
RATING_COLUMN = 9
PLAYING_MARK = "▶"

# Dragged tracks carry their ids (for dropping on a playlist or Up Next)
# and their rows (for reordering within the playlist being viewed).
TRACKS_MIME = "application/x-simple-jukebox-tracks"


def encode_tracks(track_ids: list[int], rows: list[int]) -> QMimeData:
    mime = QMimeData()
    mime.setData(TRACKS_MIME, json.dumps({"ids": track_ids, "rows": rows}).encode("utf-8"))
    return mime


def decode_tracks(mime: QMimeData) -> Optional[dict]:
    if not mime.hasFormat(TRACKS_MIME):
        return None
    try:
        payload = json.loads(bytes(mime.data(TRACKS_MIME)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
        return None
    return payload


class TrackTableModel(QAbstractTableModel):
    # Rows dropped back onto this table — only accepted while showing a
    # playlist, where it means "reorder": (moved rows, insertion row).
    rows_moved = Signal(list, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._playing_id: Optional[int] = None
        self.reorderable = False

    def set_tracks(self, tracks: list[Track]) -> None:
        self.beginResetModel()
        self._tracks = tracks
        self.endResetModel()

    def tracks(self) -> list[Track]:
        return self._tracks

    def track_at(self, row: int) -> Optional[Track]:
        return self._tracks[row] if 0 <= row < len(self._tracks) else None

    def replace_track(self, track: Track) -> None:
        for row, existing in enumerate(self._tracks):
            if existing.id == track.id:
                self._tracks[row] = track
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
                return

    def set_playing(self, track_id: Optional[int]) -> None:
        self._playing_id = track_id
        if self._tracks:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._tracks) - 1, len(COLUMNS) - 1))

    def flags(self, index):
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemIsDragEnabled
        return base | Qt.ItemIsDropEnabled if self.reorderable else base

    def supportedDragActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def supportedDropActions(self):
        return Qt.MoveAction

    def mimeTypes(self) -> list[str]:
        return [TRACKS_MIME]

    def mimeData(self, indexes) -> QMimeData:
        rows = sorted({index.row() for index in indexes if index.isValid()})
        return encode_tracks([self._tracks[row].id for row in rows], rows)

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        return self.reorderable and data.hasFormat(TRACKS_MIME)

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        payload = decode_tracks(data)
        if not self.reorderable or payload is None or not payload.get("rows"):
            return False
        if row < 0:
            row = parent.row() if parent.isValid() else len(self._tracks)
        self.rows_moved.emit(list(payload["rows"]), row)
        # The window reloads the playlist itself; returning False stops
        # the view trying to delete the "moved" source rows afterwards.
        return False

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        track = self._tracks[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            return self._display(track, column)
        if role == Qt.FontRole and track.id == self._playing_id:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.TextAlignmentRole and column in (1, 5, 7, 8):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole and column == 2:
            return track.path
        return None

    def _display(self, track: Track, column: int):
        if column == NOW_PLAYING_COLUMN:
            return PLAYING_MARK if track.id == self._playing_id else ""
        if column == 1:
            return str(track.track_number) if track.track_number else ""
        if column == 2:
            return track.title
        if column == 3:
            return track.artist
        if column == 4:
            return track.album
        if column == 5:
            return str(track.year) if track.year else ""
        if column == 6:
            return track.genre
        if column == 7:
            return format_duration(track.duration)
        if column == 8:
            return str(track.play_count) if track.play_count else ""
        if column == RATING_COLUMN:
            return "★" * track.rating
        return None
