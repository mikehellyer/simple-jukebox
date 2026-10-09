"""Table model behind the main track list."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QFont

from simple_jukebox.core.library import Track
from simple_jukebox.core.text import format_duration

COLUMNS = ("", "#", "Title", "Artist", "Album", "Year", "Genre", "Time", "Plays", "Rating")
NOW_PLAYING_COLUMN = 0
RATING_COLUMN = 9
PLAYING_MARK = "▶"


class TrackTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._playing_id: Optional[int] = None

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
