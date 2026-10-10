"""Transport icons (play, pause, previous, next) drawn as shapes.

The ⏮ ▶ ⏸ ⏭ characters come out tiny on many Linux setups whatever font
size is asked for — they're drawn from a fallback font — so the buttons
get painted icons instead: the same size and weight everywhere, sharp on
high-DPI screens, and in the theme's text colour.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF

_SCALE = 2  # draw at 2x so it stays sharp on high-DPI screens


def transport_icon(kind: str, size: int, color: QColor) -> QIcon:
    """kind: "play", "pause", "previous" or "next"."""
    pixmap = QPixmap(size * _SCALE, size * _SCALE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    s = size * _SCALE

    def triangle(left: float, right: float, pointing_right: bool = True) -> None:
        top, bottom = s * 0.18, s * 0.82
        if pointing_right:
            points = [QPointF(left, top), QPointF(right, s / 2), QPointF(left, bottom)]
        else:
            points = [QPointF(right, top), QPointF(left, s / 2), QPointF(right, bottom)]
        painter.drawPolygon(QPolygonF(points))

    def bar(left: float, width: float) -> None:
        painter.drawRoundedRect(QRectF(left, s * 0.18, width, s * 0.64), s * 0.03, s * 0.03)

    if kind == "play":
        triangle(s * 0.26, s * 0.84)
    elif kind == "pause":
        bar(s * 0.24, s * 0.18)
        bar(s * 0.58, s * 0.18)
    elif kind == "next":
        triangle(s * 0.14, s * 0.5)
        triangle(s * 0.44, s * 0.8)
        bar(s * 0.78, s * 0.09)
    elif kind == "previous":
        bar(s * 0.13, s * 0.09)
        triangle(s * 0.2, s * 0.56, pointing_right=False)
        triangle(s * 0.5, s * 0.86, pointing_right=False)
    painter.end()
    pixmap.setDevicePixelRatio(_SCALE)
    return QIcon(pixmap)
