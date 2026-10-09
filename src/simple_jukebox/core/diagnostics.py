"""Leaving a trail for crashes that Python can't catch.

A crash inside Qt or FFmpeg kills the process outright, with no Python
traceback — and an installed app has no terminal to show one anyway.
enable_crash_log() has faulthandler write every thread's Python stack to
crash.log when that happens, and note_current_track() keeps a note of
which file was playing, so a crash on one particular song can be pinned
down afterwards.
"""
from __future__ import annotations

import faulthandler
import time
from pathlib import Path
from typing import Optional

from platformdirs import user_log_dir

_crash_log_file = None


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
