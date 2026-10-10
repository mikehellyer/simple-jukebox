"""Transport controls along the top of the window: album art, now
playing, previous/play/next, a seek bar, shuffle/repeat and volume."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.text import format_duration
from simple_jukebox.gui.icons import transport_icon

ART_SIZE = 72
SKIP_BUTTON_SIZE = QSize(54, 42)
PLAY_BUTTON_SIZE = QSize(64, 50)
SKIP_ICON_SIZE = 22
PLAY_ICON_SIZE = 28
PODCAST_SKIP_STYLE = "QPushButton { font-size: 15px; font-weight: 600; }"
REPEAT_LABELS = {"off": "🔁 Off", "all": "🔁 All", "one": "🔂 One"}
NEXT_REPEAT = {"off": "all", "all": "one", "one": "off"}


class _ElidedLabel(QLabel):
    """A label that shortens long text with "…" to fit, instead of
    demanding enough width for all of it (which would stretch the whole
    window for a long song title). The full text is in the tooltip."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        elided = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), int(self.alignment()), elided)


class PlayerBar(QWidget):
    play_pause_requested = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    seek_requested = Signal(int)  # absolute position, ms
    volume_changed = Signal(int)
    shuffle_toggled = Signal(bool)
    repeat_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)

        self._art = QLabel()
        self._art.setFixedSize(ART_SIZE, ART_SIZE)
        self._art.setAlignment(Qt.AlignCenter)
        self._art.setStyleSheet("background-color: rgba(127, 127, 127, 0.15); border-radius: 4px;")
        outer.addWidget(self._art)

        transport = QHBoxLayout()
        self._previous_button = QPushButton()
        self._previous_button.clicked.connect(self.previous_requested)
        self._play_pause_button = QPushButton()
        self._play_pause_button.clicked.connect(self.play_pause_requested)
        self._next_button = QPushButton()
        self._next_button.clicked.connect(self.next_requested)
        # Big enough to hit easily and to read at a glance; play/pause
        # biggest, as on most players.
        for button in (self._previous_button, self._next_button):
            button.setFixedSize(SKIP_BUTTON_SIZE)
            button.setIconSize(QSize(SKIP_ICON_SIZE, SKIP_ICON_SIZE))
        self._play_pause_button.setFixedSize(PLAY_BUTTON_SIZE)
        self._play_pause_button.setIconSize(QSize(PLAY_ICON_SIZE, PLAY_ICON_SIZE))
        self._podcast_mode = False
        self._playing = False
        for button in (self._previous_button, self._play_pause_button, self._next_button):
            transport.addWidget(button)
        outer.addLayout(transport)

        middle = QVBoxLayout()
        self._title = _ElidedLabel("Not playing")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self._subtitle = _ElidedLabel("")
        self._subtitle.setAlignment(Qt.AlignCenter)
        self._subtitle.setStyleSheet("color: gray;")
        middle.addWidget(self._title)
        middle.addWidget(self._subtitle)

        seek_row = QHBoxLayout()
        self._elapsed = QLabel("")
        self._elapsed.setStyleSheet("color: gray; font-size: 11px;")
        self._remaining = QLabel("")
        self._remaining.setStyleSheet("color: gray; font-size: 11px;")
        self._is_scrubbing = False
        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self._seek_slider.sliderReleased.connect(self._on_slider_released)
        # Shuffle/repeat share this row, so keep the bar usefully long.
        self._seek_slider.setMinimumWidth(200)
        seek_row.addWidget(self._elapsed)
        seek_row.addWidget(self._seek_slider, stretch=1)
        seek_row.addWidget(self._remaining)

        # Shuffle/repeat sit beside the progress bar rather than up with
        # the title, so they're nearer the track list and long song
        # names get the whole width of the title line.
        seek_row.addSpacing(8)
        self._shuffle_button = QPushButton("🔀 Shuffle")
        self._shuffle_button.setCheckable(True)
        self._shuffle_button.toggled.connect(self.shuffle_toggled)
        seek_row.addWidget(self._shuffle_button)

        self._repeat = "off"
        self._repeat_button = QPushButton(REPEAT_LABELS["off"])
        self._repeat_button.clicked.connect(self._cycle_repeat)
        seek_row.addWidget(self._repeat_button)
        middle.addLayout(seek_row)
        outer.addLayout(middle, stretch=1)

        outer.addWidget(QLabel("🔈"))
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(110)
        self._volume.valueChanged.connect(self.volume_changed)
        outer.addWidget(self._volume)

        self.set_podcast_mode(False)  # draws the previous/next icons
        self.set_active(False)

    # --- state from the window -----------------------------------------

    def set_now_playing(self, title: str, subtitle: str) -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle)

    def set_art(self, data: bytes | None) -> None:
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self._art.setPixmap(
                pixmap.scaled(ART_SIZE, ART_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self._art.clear()
            self._art.setText("♪")

    def set_active(self, active: bool) -> None:
        self._previous_button.setEnabled(active)
        self._next_button.setEnabled(active)
        self._seek_slider.setEnabled(active)
        if not active:
            self.set_now_playing("Not playing", "")
            self.set_art(None)
            self.set_progress(0, 0)
            self.set_playing(False)

    def _icon(self, kind: str, size: int):
        return transport_icon(kind, size, self.palette().color(self.foregroundRole()))

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._play_pause_button.setIcon(self._icon("pause" if playing else "play", PLAY_ICON_SIZE))
        self._play_pause_button.setToolTip("Pause" if playing else "Play")

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        if duration_ms > 0:
            self._seek_slider.setRange(0, duration_ms)
            if not self._is_scrubbing:
                self._seek_slider.setValue(position_ms)
            self._elapsed.setText(format_duration(position_ms / 1000))
            self._remaining.setText("-" + format_duration(max(0, duration_ms - position_ms) / 1000))
        else:
            self._seek_slider.setRange(0, 0)
            self._elapsed.setText("")
            self._remaining.setText("")

    def set_podcast_mode(self, podcast: bool) -> None:
        """While an episode plays, ⏮/⏭ skip back 15s / forward 30s — the
        usual podcast controls — instead of changing track."""
        self._podcast_mode = podcast
        style = PODCAST_SKIP_STYLE if podcast else ""
        self._previous_button.setStyleSheet(style)
        self._next_button.setStyleSheet(style)
        if podcast:
            self._previous_button.setIcon(QIcon())
            self._previous_button.setText("−15")
            self._previous_button.setToolTip("Back 15 seconds")
            self._next_button.setIcon(QIcon())
            self._next_button.setText("+30")
            self._next_button.setToolTip("Forward 30 seconds")
        else:
            self._previous_button.setText("")
            self._previous_button.setIcon(self._icon("previous", SKIP_ICON_SIZE))
            self._previous_button.setToolTip("Previous track")
            self._next_button.setText("")
            self._next_button.setIcon(self._icon("next", SKIP_ICON_SIZE))
            self._next_button.setToolTip("Next track")

    def changeEvent(self, event) -> None:
        # Light/dark theme switched: repaint the icons in the new text colour.
        if event.type() == event.Type.PaletteChange:
            self.set_playing(self._playing)
            self.set_podcast_mode(self._podcast_mode)
        super().changeEvent(event)

    def set_volume(self, volume: int) -> None:
        self._volume.setValue(volume)

    def set_shuffle(self, shuffle: bool) -> None:
        self._shuffle_button.setChecked(shuffle)

    def set_repeat(self, repeat: str) -> None:
        self._repeat = repeat
        self._repeat_button.setText(REPEAT_LABELS.get(repeat, REPEAT_LABELS["off"]))

    # --- internals ------------------------------------------------------

    def _cycle_repeat(self) -> None:
        self.set_repeat(NEXT_REPEAT[self._repeat])
        self.repeat_changed.emit(self._repeat)

    def _on_slider_pressed(self) -> None:
        self._is_scrubbing = True

    def _on_slider_released(self) -> None:
        self._is_scrubbing = False
        self.seek_requested.emit(self._seek_slider.value())
