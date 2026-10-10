"""System media controls: keyboard media keys and the desktop's
now-playing controls.

On Linux this is MPRIS (core.mpris) — media keys, the panel/lock-screen
media widget and playerctl all work even when the window isn't focused,
and they show the current song and its art. Elsewhere (or if the session
bus isn't available) the media keys still work while the app is focused,
via ordinary Qt shortcuts.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional

from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from simple_jukebox.core.mpris import (
    LOOP_FOR_REPEAT,
    REPEAT_FOR_LOOP,
    MprisService,
    MprisState,
    build_metadata,
)


class MediaControls(QObject):
    play_pause = Signal()
    play = Signal()
    pause = Signal()
    stop = Signal()
    next = Signal()
    previous = Signal()
    seek_by = Signal(int)  # ms, relative
    seek_to = Signal(int)  # ms, absolute
    raise_window = Signal()
    quit = Signal()
    shuffle_requested = Signal(bool)
    repeat_requested = Signal(str)  # "off" | "all" | "one"
    volume_requested = Signal(int)  # 0–100

    # Carries commands from the D-Bus thread to the GUI thread.
    _command = Signal(str, object)

    def __init__(self, window):
        super().__init__(window)
        self._state = MprisState()
        self._service: Optional[MprisService] = None
        self._art_file: Optional[Path] = None
        self._command.connect(self._dispatch, Qt.QueuedConnection)
        if sys.platform.startswith("linux"):
            service = MprisService(self._state, lambda name, arg: self._command.emit(name, arg))
            if service.start():
                self._service = service
        if self._service is None:
            self._add_media_key_shortcuts(window)

    @property
    def system_wide(self) -> bool:
        return self._service is not None

    def _add_media_key_shortcuts(self, window) -> None:
        keys = {
            Qt.Key_MediaTogglePlayPause: self.play_pause,
            Qt.Key_MediaPlay: self.play_pause,  # many keyboards send Play for the play/pause key
            Qt.Key_MediaPause: self.pause,
            Qt.Key_MediaStop: self.stop,
            Qt.Key_MediaNext: self.next,
            Qt.Key_MediaPrevious: self.previous,
        }
        for key, signal in keys.items():
            shortcut = QShortcut(QKeySequence(key), window)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(signal)

    def _dispatch(self, name: str, argument) -> None:
        simple = {
            "play_pause": self.play_pause, "play": self.play, "pause": self.pause, "stop": self.stop,
            "next": self.next, "previous": self.previous, "raise": self.raise_window, "quit": self.quit,
        }
        if name in simple:
            simple[name].emit()
        elif name == "seek":
            self.seek_by.emit(int(argument) // 1000)
        elif name == "set_position":
            track_id, position_us = argument
            current = self._state.player["Metadata"][1].get("mpris:trackid", ("o", ""))[1]
            if track_id == current:  # the spec: ignore requests for a track that's no longer playing
                self.seek_to.emit(int(position_us) // 1000)
        elif name == "set_shuffle":
            self.shuffle_requested.emit(bool(argument))
        elif name == "set_loop" and argument in REPEAT_FOR_LOOP:
            self.repeat_requested.emit(REPEAT_FOR_LOOP[argument])
        elif name == "set_volume":
            self.volume_requested.emit(max(0, min(100, round(float(argument) * 100))))

    # --- what the player tells us ---------------------------------------------

    def _update(self, **changes) -> None:
        changed = self._state.update(**changes)
        if self._service is not None:
            self._service.announce(changed)

    def set_now_playing(
        self,
        track_path: Optional[str],
        title: str = "",
        artist: str = "",
        album: str = "",
        album_artist: str = "",
        length_ms: int = 0,
        art: Optional[bytes] = None,
        track_number: Optional[int] = None,
        url: str = "",
    ) -> None:
        """track_path: an MPRIS object path naming the item (see
        core.mpris.track_object_path), or None when nothing is loaded."""
        art_url = self._write_art(art) if track_path else ""
        self._update(
            Metadata=build_metadata(
                track_path,
                title=title,
                artists=[artist] if artist else None,
                album=album,
                album_artists=[album_artist] if album_artist else None,
                length_us=length_ms * 1000,
                art_url=art_url,
                track_number=track_number,
                url=url,
            ),
            CanPause=track_path is not None,
            CanSeek=track_path is not None,
        )
        if track_path is None:
            self._update(PlaybackStatus="Stopped")
            self._state.set_position(0)

    def set_length(self, length_ms: int) -> None:
        """The real length, once the player knows it (tags can be off)."""
        metadata = dict(self._state.player["Metadata"][1])
        if "xesam:title" in metadata and length_ms > 0:
            metadata["mpris:length"] = ("x", int(length_ms) * 1000)
            self._update(Metadata=metadata)

    def set_playing(self, playing: bool, loaded: bool) -> None:
        self._update(PlaybackStatus="Playing" if playing else ("Paused" if loaded else "Stopped"))

    def set_can_skip(self, can_next: bool, can_previous: bool) -> None:
        self._update(CanGoNext=can_next, CanGoPrevious=can_previous)

    def set_shuffle(self, shuffle: bool) -> None:
        self._update(Shuffle=shuffle)

    def set_repeat(self, repeat: str) -> None:
        self._update(LoopStatus=LOOP_FOR_REPEAT.get(repeat, "None"))

    def set_volume(self, volume: int) -> None:
        self._update(Volume=max(0.0, min(1.0, volume / 100)))

    def set_position(self, position_ms: int) -> None:
        self._state.set_position(position_ms * 1000)

    def seeked(self, position_ms: int) -> None:
        """After a jump in position (not normal playback), so the desktop's
        progress display follows."""
        if self._service is not None:
            self._service.seeked(position_ms * 1000)
        else:
            self._state.set_position(position_ms * 1000)

    def _write_art(self, art: Optional[bytes]) -> str:
        """Desktops want cover art as a file URL. One file per cover (so
        the URL changes when the art does); the previous one is removed."""
        if not art:
            return ""
        folder = Path(user_cache_dir("Simple-Jukebox")) / "now-playing"
        path = folder / f"{hashlib.sha1(art).hexdigest()[:16]}.img"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(art)
            if self._art_file is not None and self._art_file != path:
                self._art_file.unlink(missing_ok=True)
        except OSError:
            return ""
        self._art_file = path
        return path.as_uri()

    def shutdown(self) -> None:
        if self._service is not None:
            self._service.stop()
        if self._art_file is not None:
            try:
                self._art_file.unlink(missing_ok=True)
            except OSError:
                pass
