"""The "Up Next" side panel: what's playing now and what plays after it.

Double-click jumps to a track, Delete removes it, dragging reorders, and
tracks dragged in from the main table are queued where they're dropped.
Rows are always addressed by their offset in Up Next (0 = the very next
track), matching PlayQueue's editing methods.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.library import Track
from simple_jukebox.core.text import format_duration
from simple_jukebox.gui.track_model import TRACKS_MIME, decode_tracks

# A queue loaded from "All Music" can be the whole library; listing tens
# of thousands of rows is slow and not useful, so the panel shows the
# head of it and a count of the rest.
MAX_SHOWN = 500


class _UpNextList(QListWidget):
    move_requested = Signal(int, int)  # from offset, insertion offset (pre-move)
    tracks_dropped = Signal(list, int)  # track ids, insertion offset

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setStyleSheet(
            "QListWidget::item { padding: 4px 2px; border-bottom: 1px solid rgba(127, 127, 127, 0.18); }"
        )

    def _insertion_row(self, event) -> int:
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        row = self.row(item)
        rect = self.visualItemRect(item)
        return row + 1 if pos.y() > rect.center().y() else row

    def dragEnterEvent(self, event) -> None:
        if event.source() is self or event.mimeData().hasFormat(TRACKS_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)  # draws the drop indicator
        event.setDropAction(Qt.MoveAction if event.source() is self else Qt.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        # Never let QListWidget move its own items — the window rebuilds
        # the list from the queue once the queue itself has changed.
        to_row = self._insertion_row(event)
        if event.source() is self:
            from_row = self.currentRow()
            if from_row >= 0:
                self.move_requested.emit(from_row, to_row)
        else:
            payload = decode_tracks(event.mimeData())
            if payload is not None:
                self.tracks_dropped.emit(list(payload["ids"]), to_row)
        event.setDropAction(Qt.IgnoreAction)
        event.accept()


class UpNextPanel(QWidget):
    jump_requested = Signal(int)
    remove_requested = Signal(int)
    move_requested = Signal(int, int)  # from offset, to offset (post-removal, PlayQueue style)
    tracks_dropped = Signal(list, int)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        heading = QLabel("NOW PLAYING")
        heading.setStyleSheet("color: gray; font-size: 10px; font-weight: 600;")
        layout.addWidget(heading)
        self._now_playing = QLabel("Nothing")
        self._now_playing.setWordWrap(True)
        self._now_playing.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._now_playing)

        header_row = QHBoxLayout()
        self._up_next_heading = QLabel("UP NEXT")
        self._up_next_heading.setStyleSheet("color: gray; font-size: 10px; font-weight: 600;")
        header_row.addWidget(self._up_next_heading, stretch=1)
        self._clear_button = QPushButton("Clear")
        self._clear_button.setFlat(True)
        self._clear_button.clicked.connect(self.clear_requested)
        header_row.addWidget(self._clear_button)
        layout.addLayout(header_row)

        self._list = _UpNextList()
        self._list.itemDoubleClicked.connect(lambda item: self.jump_requested.emit(self._list.row(item)))
        self._list.move_requested.connect(self._on_move_requested)
        self._list.tracks_dropped.connect(self.tracks_dropped)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self._list, stretch=1)

        self._more = QLabel("")
        self._more.setStyleSheet("color: gray;")
        self._more.hide()
        layout.addWidget(self._more)

        hint = QLabel("Drag songs here to queue them")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        for key in (QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)):
            shortcut = QShortcut(key, self._list)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(self._remove_selected)

    def set_queue(self, current: Track | None, upcoming: list[Track | None]) -> None:
        """upcoming may hold None for ids no longer in the library — they
        still occupy an offset, so they're listed (greyed) to keep rows
        and queue offsets lined up."""
        if current is not None:
            self._now_playing.setText(f"{current.title}\n{current.artist}")
        else:
            self._now_playing.setText("Nothing")

        selected = self._list.currentRow()
        self._list.clear()
        for track in upcoming[:MAX_SHOWN]:
            if track is None:
                item = QListWidgetItem("(missing file)")
                item.setForeground(Qt.gray)
            else:
                item = QListWidgetItem(f"{track.title}\n{track.artist} · {format_duration(track.duration)}")
                item.setToolTip(track.path)
            self._list.addItem(item)
        if 0 <= selected < self._list.count():
            self._list.setCurrentRow(selected)

        extra = len(upcoming) - MAX_SHOWN
        self._more.setVisible(extra > 0)
        self._more.setText(f"…and {extra:,} more")
        self._up_next_heading.setText(f"UP NEXT ({len(upcoming):,})" if upcoming else "UP NEXT")
        self._clear_button.setEnabled(bool(upcoming))

    def _on_move_requested(self, from_row: int, to_row: int) -> None:
        # The list reports an insertion point counted before the dragged
        # row is lifted out; PlayQueue.move_upcoming wants it after.
        if to_row > from_row:
            to_row -= 1
        if to_row != from_row:
            self.move_requested.emit(from_row, to_row)

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self.remove_requested.emit(row)

    def _show_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        row = self._list.row(item)
        menu = QMenu(self)
        menu.addAction("Play Now", lambda: self.jump_requested.emit(row))
        menu.addAction("Move to Top", lambda: self.move_requested.emit(row, 0))
        menu.addAction("Remove from Up Next", lambda: self.remove_requested.emit(row))
        menu.exec(self._list.viewport().mapToGlobal(pos))
