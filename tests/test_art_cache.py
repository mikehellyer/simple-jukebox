import os
import time

from simple_jukebox.core.art_cache import cache_key, no_art_marker, thumbnail_path


def test_cache_key_changes_when_track_or_folder_changes(tmp_path):
    track = tmp_path / "Album" / "01.flac"
    track.parent.mkdir()
    track.write_bytes(b"x")
    first = cache_key(str(track))
    assert first == cache_key(str(track))

    future = time.time() + 10
    os.utime(track, (future, future))
    second = cache_key(str(track))
    assert second != first

    (track.parent / "cover.jpg").write_bytes(b"jpg")  # changes the folder's mtime
    os.utime(track.parent, (future + 10, future + 10))
    assert cache_key(str(track)) != second


def test_cache_key_for_missing_track_is_none(tmp_path):
    assert cache_key(str(tmp_path / "gone.mp3")) is None


def test_cache_paths_are_sharded(tmp_path):
    assert thumbnail_path(tmp_path, "abcdef").parent.name == "ab"
    assert no_art_marker(tmp_path, "abcdef").name == "abcdef.none"
