"""Transport controls along the top of the window: album art, now
playing, previous/play/next, a seek bar, shuffle/repeat and volume."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from simple_jukebox.core.text import format_duration

ART_SIZE = 72
REPEAT_LABELS = {"off": "🔁 Off", "all": "🔁 All", "one": "🔂 One"}
NEXT_REPEAT = {"off": "all", "all": "one", "one": "off"}


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
        self._previous_button = QPushButton("⏮")
        self._previous_button.clicked.connect(self.previous_requested)
        self._play_pause_button = QPushButton("▶")
        self._play_pause_button.clicked.connect(self.play_pause_requested)
        self._next_button = QPushButton("⏭")
        self._next_button.clicked.connect(self.next_requested)
        for button in (self._previous_button, self._play_pause_button, self._next_button):
            button.setFixedWidth(44)
            transport.addWidget(button)
        outer.addLayout(transport)

        middle = QVBoxLayout()
        self._title = QLabel("Not playing")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self._subtitle = QLabel("")
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
        seek_row.addWidget(self._elapsed)
        seek_row.addWidget(self._seek_slider, stretch=1)
        seek_row.addWidget(self._remaining)
        middle.addLayout(seek_row)
        outer.addLayout(middle, stretch=1)

        self._shuffle_button = QPushButton("🔀 Shuffle")
        self._shuffle_button.setCheckable(True)
        self._shuffle_button.toggled.connect(self.shuffle_toggled)
        outer.addWidget(self._shuffle_button)

        self._repeat = "off"
        self._repeat_button = QPushButton(REPEAT_LABELS["off"])
        self._repeat_button.clicked.connect(self._cycle_repeat)
        outer.addWidget(self._repeat_button)

        outer.addWidget(QLabel("🔈"))
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(110)
        self._volume.valueChanged.connect(self.volume_changed)
        outer.addWidget(self._volume)

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

    def set_playing(self, playing: bool) -> None:
        self._play_pause_button.setText("⏸" if playing else "▶")

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
