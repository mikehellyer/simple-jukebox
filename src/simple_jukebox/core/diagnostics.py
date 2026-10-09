"""Leaving a trail for crashes that Python can't catch.

A crash inside Qt or FFmpeg kills the process outright, with no Python
traceback — and an installed app has no terminal to show one anyway.
enable_crash_log() has faulthandler write every thread's Python stack to
crash.log when that happens, and note_current_track() keeps a note of
which file was playing, so a crash on one particular song can be pinned
down afterwards.

Not every abrupt exit is a crash, though: Qt deliberately exit()s after
printing one line when it loses its display connection (e.g. a Wayland
protocol error), which leaves crash.log empty. enable_app_log() catches
those by sending the process's stderr — Python's and Qt/FFmpeg's — to
app.log, bracketed by "started" / "exited" lines. A log with no
"exited" line means the process was killed from outside.
"""
from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import time
from pathlib import Path
from typing import Optional

from platformdirs import user_log_dir

_crash_log_file = None
_app_log_file = None

# app.log gets every FFmpeg file-header dump, so it's started afresh once
# it passes this size rather than growing forever.
APP_LOG_MAX_BYTES = 2_000_000


def log_dir() -> Path:
    return Path(user_log_dir("Simple-Jukebox"))


def enable_crash_log(directory: Optional[Path] = None) -> Optional[Path]:
    global _crash_log_file
    directory = directory or log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "crash.log"
        _crash_log_file = open(path, "a", buffering=1)  # kept open for the process's life
    except OSError:
        return None
    faulthandler.enable(file=_crash_log_file, all_threads=True)
    return path


def note_current_track(path: str, directory: Optional[Path] = None) -> None:
    """Record the file being played. Never raises — diagnostics must not
    be able to break playback."""
    directory = directory or log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "last_track.txt").write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{path}\n")
    except OSError:
        pass


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def enable_app_log(version: str, directory: Optional[Path] = None, redirect_stderr: Optional[bool] = None) -> Optional[Path]:
    """Start app.log for this run. When redirect_stderr (default: only in
    the installed, frozen app — a terminal run keeps its output on
    screen), file descriptor 2 itself is pointed at the log so native
    Qt/FFmpeg messages land there too, not just Python's."""
    global _app_log_file
    directory = directory or log_dir()
    if redirect_stderr is None:
        redirect_stderr = bool(getattr(sys, "frozen", False))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "app.log"
        if path.exists() and path.stat().st_size > APP_LOG_MAX_BYTES:
            path.replace(directory / "app.previous.log")
        _app_log_file = open(path, "a", buffering=1)
    except OSError:
        return None

    _app_log_file.write(f"\n{_stamp()} started Simple-Jukebox {version} (pid {os.getpid()})\n")
    if redirect_stderr:
        try:
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            pass
        os.dup2(_app_log_file.fileno(), 2)
        sys.stderr = _app_log_file
    atexit.register(_log_exit)
    return path


def _log_exit() -> None:
    if _app_log_file is not None:
        try:
            _app_log_file.write(f"{_stamp()} exited normally\n")
            _app_log_file.flush()
        except (OSError, ValueError):
            pass


def log_event(message: str) -> None:
    """A line in app.log (e.g. the Qt event loop's exit code). Never raises."""
    if _app_log_file is not None:
        try:
            _app_log_file.write(f"{_stamp()} {message}\n")
        except (OSError, ValueError):
            pass
