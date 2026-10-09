"""The album grid: one cover per album with its title and artist beneath,
like Strawberry's or iTunes' album view.

Covers load on a background thread (embedded art, else cover.jpg/folder.jpg)
and are cached as small thumbnails on disk (see core.art_cache), so the grid
appears immediately and fills in — instantly on later visits.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from simple_jukebox.core.art_cache import cache_key, default_cache_dir, no_art_marker, thumbnail_path
from simple_jukebox.core.artwork import find_art
from simple_jukebox.core.library import AlbumSummary, Library
from simple_jukebox.gui.track_model import encode_tracks

COVER_SIZE = 160  # shown size, px
THUMBNAIL_SIZE = 320  # cached size — sharp on high-DPI screens too
CELL_WIDTH = COVER_SIZE + 24
CELL_HEIGHT = COVER_SIZE + 52
ALBUM_ROLE = Qt.UserRole


class _ArtLoader(QObject):
    """One background thread that turns album cover paths into thumbnails.

    Requests are tagged with a generation number; when the grid is
    repopulated the generation moves on and stale requests are dropped."""

    art_ready = Signal(int, object, QImage)  # generation, album key, image

    def __init__(self, cache_dir: Optional[Path] = None):
        super().__init__()
        self._cache_dir = cache_dir or default_cache_dir()
        self._requests: "queue.Queue[tuple[int, tuple, str]]" = queue.Queue()
        self._generation = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="album-art", daemon=True)
        self._thread.start()

    def request_all(self, albums: list[AlbumSummary]) -> int:
        self._generation += 1
        for album in albums:
            self._requests.put((self._generation, album.key, album.cover_path))
        return self._generation

    def stop(self) -> None:
        self._stopped = True
        self._requests.put((-1, (), ""))

    def _run(self) -> None:
        while not self._stopped:
            generation, key, path = self._requests.get()
            if self._stopped or generation != self._generation:
                continue
            image = self._load(path)
            if image is not None and not self._stopped:
                self.art_ready.emit(generation, key, image)

    def _load(self, track_path: str) -> Optional[QImage]:
        # QImage (unlike QPixmap) is safe to use off the GUI thread.
        key = cache_key(track_path)
        if key is None:
            return None
        cached = thumbnail_path(self._cache_dir, key)
        if cached.exists():
            image = QImage(str(cached))
            if not image.isNull():
                return image
        marker = no_art_marker(self._cache_dir, key)
        if marker.exists():
            return None
        data = find_art(Path(track_path))
        image = QImage.fromData(data) if data else QImage()
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            if image.isNull():
                marker.touch()
                return None
            image = image.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            image.save(str(cached), "JPG", 88)
        except OSError:
            pass  # caching is only a speed-up
        return None if image.isNull() else image


class AlbumGridModel(QAbstractListModel):
    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self._library = library
        self._albums: list[AlbumSummary] = []
        self._row_of: dict[tuple, int] = {}
        self._art: dict[tuple, QPixmap] = {}  # survives repopulating, so covers don't flicker
        self._generation = 0
        self._loader = _ArtLoader()
        self._loader.art_ready.connect(self._on_art_ready, Qt.QueuedConnection)

    def set_albums(self, albums: list[AlbumSummary]) -> None:
        self.beginResetModel()
        self._albums = albums
        self._row_of = {album.key: row for row, album in enumerate(albums)}
        self.endResetModel()
        missing = [album for album in albums if album.key not in self._art]
        self._generation = self._loader.request_all(missing)

    def forget_art(self) -> None:
        """After a rescan: covers may have changed."""
        self._art.clear()

    def album_at(self, row: int) -> Optional[AlbumSummary]:
        return self._albums[row] if 0 <= row < len(self._albums) else None

    def row_of(self, key: tuple) -> Optional[int]:
        return self._row_of.get(key)

    def art_for(self, key: tuple) -> Optional[QPixmap]:
        return self._art.get(key)

    def shutdown(self) -> None:
        self._loader.stop()

    def _on_art_ready(self, generation: int, key: tuple, image: QImage) -> None:
        if generation != self._generation:
            return
        self._art[key] = QPixmap.fromImage(image)
        row = self._row_of.get(key)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.DecorationRole])

    # --- Qt model API ---------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._albums)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        album = self._albums[index.row()]
        if role == Qt.DisplayRole:
            return album.album
        if role == ALBUM_ROLE:
            return album
        if role == Qt.DecorationRole:
            return self._art.get(album.key)
        if role == Qt.ToolTipRole:
            year = f" ({album.year})" if album.year else ""
            songs = f"{album.track_count} song{'s' if album.track_count != 1 else ''}"
            return f"{album.album}{year}\n{album.album_artist}\n{songs}"
        return None

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemIsDragEnabled if index.isValid() else flags

    def mimeTypes(self) -> list[str]:
        from simple_jukebox.gui.track_model import TRACKS_MIME

        return [TRACKS_MIME]

    def mimeData(self, indexes):
        # Dragging albums carries their songs, in album order, so they can
        # be dropped on a playlist or the Up Next panel like table rows.
        ids = []
        for row in sorted({index.row() for index in indexes if index.isValid()}):
            album = self._albums[row]
            ids.extend(t.id for t in self._library.album_tracks(*album.key))
        return encode_tracks(ids, [])


class _AlbumDelegate(QStyledItemDelegate):
    """Cover on top, album title (bold) and artist (grey) beneath, both
    shortened with "…" to fit."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(CELL_WIDTH, CELL_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        album: AlbumSummary = index.data(ALBUM_ROLE)
        pixmap: Optional[QPixmap] = index.data(Qt.DecorationRole)
        rect = option.rect
        palette = option.palette
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            highlight = QColor(palette.color(QPalette.Highlight))
            highlight.setAlpha(60)
            painter.setPen(Qt.NoPen)
            painter.setBrush(highlight)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)

        cover = QRect(rect.x() + (rect.width() - COVER_SIZE) // 2, rect.y() + 8, COVER_SIZE, COVER_SIZE)
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(COVER_SIZE, COVER_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = cover.x() + (COVER_SIZE - scaled.width()) // 2
            y = cover.y() + (COVER_SIZE - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            # Placeholder: a soft square with a note, like an empty sleeve.
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(127, 127, 127, 40))
            painter.drawRoundedRect(cover, 4, 4)
            note_font = QFont(option.font)
            note_font.setPointSizeF(note_font.pointSizeF() * 3)
            painter.setFont(note_font)
            painter.setPen(QColor(127, 127, 127, 150))
            painter.drawText(cover, Qt.AlignCenter, "♪")

        text_width = rect.width() - 12
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(palette.color(QPalette.Text))
        metrics = painter.fontMetrics()
        title_rect = QRect(rect.x() + 6, cover.bottom() + 6, text_width, metrics.height())
        painter.drawText(
            title_rect, Qt.AlignHCenter, metrics.elidedText(album.album, Qt.ElideRight, text_width)
        )

        painter.setFont(option.font)
        painter.setPen(QColor(128, 128, 128))
        metrics = painter.fontMetrics()
        artist_rect = QRect(rect.x() + 6, title_rect.bottom() + 2, text_width, metrics.height())
        painter.drawText(
            artist_rect, Qt.AlignHCenter, metrics.elidedText(album.album_artist, Qt.ElideRight, text_width)
        )
        painter.restore()


class AlbumGridView(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QSize(CELL_WIDTH + 8, CELL_HEIGHT + 8))
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setItemDelegate(_AlbumDelegate(self))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
