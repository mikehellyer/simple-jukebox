"""The "Up Next" queue: which track plays after this one, honouring
shuffle and repeat. Holds plain track ids — no Qt, no I/O — so the
next/previous rules can be tested directly.
"""
from __future__ import annotations

import random
from typing import Optional


class PlayQueue:
    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._tracks: list[int] = []  # in the order the user saw them
        self._order: list[int] = []  # indexes into _tracks, in play order
        self._position = -1  # index into _order
        self.shuffle = False
        self.repeat = "off"  # "off" | "all" | "one"

    def load(self, track_ids: list[int], start_index: int = 0, shuffle: Optional[bool] = None) -> Optional[int]:
        """Replace the queue (e.g. the current track list) and start at start_index."""
        if shuffle is not None:
            self.shuffle = shuffle
        self._tracks = list(track_ids)
        if not self._tracks:
            self._order, self._position = [], -1
            return None
        start_index = max(0, min(start_index, len(self._tracks) - 1))
        self._rebuild_order(start_index)
        return self.current()

    def _rebuild_order(self, current_index: int) -> None:
        indexes = list(range(len(self._tracks)))
        if self.shuffle:
            # The current track stays first; everything else is shuffled after it.
            indexes.remove(current_index)
            self._rng.shuffle(indexes)
            self._order = [current_index] + indexes
            self._position = 0
        else:
            self._order = indexes
            self._position = current_index

    def set_shuffle(self, shuffle: bool) -> None:
        if shuffle == self.shuffle:
            return
        self.shuffle = shuffle
        if self._position >= 0:
            self._rebuild_order(self._order[self._position])

    def __len__(self) -> int:
        return len(self._tracks)

    def current(self) -> Optional[int]:
        if self._position < 0:
            return None
        return self._tracks[self._order[self._position]]

    def next(self, user_requested: bool = False) -> Optional[int]:
        """Advance and return the new current track, or None at the end.

        Repeat-one only replays automatically at the end of a track — a
        deliberate "Next" click still moves on.
        """
        if self._position < 0:
            return None
        if self.repeat == "one" and not user_requested:
            return self.current()
        if self._position + 1 < len(self._order):
            self._position += 1
            return self.current()
        if self.repeat in ("all", "one"):
            if self.shuffle:
                self._rng.shuffle(self._order)
            self._position = 0
            return self.current()
        return None

    def previous(self) -> Optional[int]:
        if self._position < 0:
            return None
        if self._position > 0:
            self._position -= 1
        elif self.repeat == "all":
            self._position = len(self._order) - 1
        return self.current()
