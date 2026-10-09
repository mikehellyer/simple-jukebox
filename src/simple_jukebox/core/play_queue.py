"""The play queue behind "Up Next": which track plays after this one,
honouring shuffle and repeat, plus the user's own edits — "Play Next",
"Add to Up Next", removing or reordering upcoming tracks.

Holds plain track ids — no Qt, no I/O — so the rules can be tested
directly. Each queued item is an "entry" with its own key, so the same
track can be queued twice and each copy removed or moved independently.
"""
from __future__ import annotations

import itertools
import random
from typing import Iterable, Optional


class PlayQueue:
    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._keys = itertools.count()
        self._track_of: dict[int, int] = {}  # entry key → track id
        self._items: list[int] = []  # entry keys, in play order
        self._natural: list[int] = []  # entry keys, in the order the user saw them
        self._position = -1  # index into _items of the current entry
        self._finished = False  # played off the end; nothing is current
        self.shuffle = False
        self.repeat = "off"  # "off" | "all" | "one"

    def _new_entries(self, track_ids: Iterable[int]) -> list[int]:
        entries = []
        for track_id in track_ids:
            key = next(self._keys)
            self._track_of[key] = track_id
            entries.append(key)
        return entries

    def load(self, track_ids: list[int], start_index: int = 0, shuffle: Optional[bool] = None) -> Optional[int]:
        """Replace the queue (e.g. the current track list) and start at start_index."""
        if shuffle is not None:
            self.shuffle = shuffle
        self._track_of.clear()
        self._finished = False
        self._natural = self._new_entries(track_ids)
        if not self._natural:
            self._items, self._position = [], -1
            return None
        start_index = max(0, min(start_index, len(self._natural) - 1))
        self._items = list(self._natural)
        self._position = start_index
        if self.shuffle:
            self._shuffle_around_current()
        return self.current()

    def _shuffle_around_current(self) -> None:
        """Current entry first, then every other entry in random order."""
        current = self._items[self._position]
        rest = [entry for entry in self._natural if entry != current]
        self._rng.shuffle(rest)
        self._items = [current] + rest
        self._position = 0

    def set_shuffle(self, shuffle: bool) -> None:
        if shuffle == self.shuffle:
            return
        self.shuffle = shuffle
        if self._position < 0:
            return
        if shuffle:
            self._shuffle_around_current()
        else:
            current = self._items[self._position]
            self._items = list(self._natural)
            self._position = self._items.index(current)

    def __len__(self) -> int:
        return len(self._items)

    def current(self) -> Optional[int]:
        if self._finished or not 0 <= self._position < len(self._items):
            return None
        return self._track_of[self._items[self._position]]

    def upcoming(self) -> list[int]:
        """Track ids still to play, in order (what the Up Next panel lists)."""
        return [self._track_of[entry] for entry in self._items[self._position + 1:]]

    def next(self, user_requested: bool = False) -> Optional[int]:
        """Advance and return the new current track, or None at the end.

        Repeat-one only replays automatically at the end of a track — a
        deliberate "Next" click still moves on.
        """
        if not self._items:
            return None
        if self.repeat == "one" and not user_requested and self.current() is not None:
            return self.current()
        if self._position + 1 < len(self._items):
            self._position += 1
            self._finished = False
            return self.current()
        if self.repeat in ("all", "one"):
            if self.shuffle:
                self._rng.shuffle(self._items)
            self._position = 0
            self._finished = False
            return self.current()
        # Ran off the end: stay parked on the last item, so anything
        # queued later still counts as upcoming and plays next.
        self._finished = True
        return None

    def previous(self) -> Optional[int]:
        if not self._items:
            return None
        if self._finished:
            self._finished = False
        elif self._position > 0:
            self._position -= 1
        elif self.repeat == "all":
            self._position = len(self._items) - 1
        return self.current()

    # --- editing Up Next -----------------------------------------------

    def _natural_insert_point(self) -> int:
        """Where "play next" entries go in the natural order: just after
        the current entry, so turning shuffle off keeps them next."""
        if 0 <= self._position < len(self._items):
            return self._natural.index(self._items[self._position]) + 1
        return len(self._natural)

    def play_next(self, track_ids: list[int]) -> None:
        """Queue tracks to play straight after the current one, in order."""
        entries = self._new_entries(track_ids)
        insert_at = min(self._position + 1, len(self._items))
        natural_at = self._natural_insert_point()
        self._items[insert_at:insert_at] = entries
        self._natural[natural_at:natural_at] = entries

    def add(self, track_ids: list[int]) -> None:
        """Queue tracks at the very end of Up Next."""
        entries = self._new_entries(track_ids)
        self._items.extend(entries)
        self._natural.extend(entries)

    def _upcoming_index(self, offset: int) -> Optional[int]:
        index = self._position + 1 + offset
        if offset < 0 or not 0 <= index < len(self._items):
            return None
        return index

    def remove_upcoming(self, offset: int) -> None:
        """Remove the offset'th upcoming track (0 = the very next one)."""
        index = self._upcoming_index(offset)
        if index is None:
            return
        entry = self._items.pop(index)
        self._natural.remove(entry)
        del self._track_of[entry]

    def move_upcoming(self, from_offset: int, to_offset: int) -> None:
        """Reorder Up Next (both offsets counted from the very next track)."""
        index = self._upcoming_index(from_offset)
        if index is None:
            return
        entry = self._items.pop(index)
        upcoming_count = len(self._items) - (self._position + 1)
        to_offset = max(0, min(to_offset, upcoming_count))
        self._items.insert(self._position + 1 + to_offset, entry)

    def jump_to(self, offset: int) -> Optional[int]:
        """Skip ahead to the offset'th upcoming track and make it current."""
        index = self._upcoming_index(offset)
        if index is None:
            return self.current()
        self._position = index
        self._finished = False
        return self.current()

    def clear_upcoming(self) -> None:
        keep = set(self._items[: self._position + 1])
        for entry in self._items[self._position + 1:]:
            del self._track_of[entry]
        self._items = self._items[: self._position + 1]
        self._natural = [entry for entry in self._natural if entry in keep]
