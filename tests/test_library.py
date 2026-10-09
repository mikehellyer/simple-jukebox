from pathlib import Path

from simple_jukebox.core.library import Library
from simple_jukebox.core.tags import TrackInfo


def _fake_reader(tags_by_name):
    def read(path: Path):
        artist, album, title, number = tags_by_name[path.name]
        return TrackInfo(
            path=str(path), title=title, artist=artist, album=album,
            album_artist=artist, track_number=number, duration=180.0,
        )
    return read


def _touch(folder: Path, name: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


TAGS = {
    "a1.mp3": ("Alpha", "Debut", "Song B", 2),
    "a2.mp3": ("Alpha", "Debut", "Song A", 1),
    "b1.flac": ("Beta", "Live", "Encore", 1),
}


def _library_with_files(tmp_path):
    music = tmp_path / "music"
    for name in TAGS:
        _touch(music, name)
    _touch(music, "cover.jpg")  # not audio — ignored
    library = Library(tmp_path / "lib.sqlite3")
    result = library.scan([str(music)], read=_fake_reader(TAGS))
    return library, music, result


def test_scan_adds_audio_files_only(tmp_path):
    library, _, result = _library_with_files(tmp_path)
    assert result.added == 3
    assert library.track_count() == 3


def test_rescan_skips_unchanged_and_drops_missing(tmp_path):
    library, music, _ = _library_with_files(tmp_path)
    (music / "b1.flac").unlink()

    result = library.scan([str(music)], read=_fake_reader(TAGS))

    assert result.unchanged == 2
    assert result.removed == 1
    assert library.artists() == ["Alpha"]


def test_tracks_are_in_album_order(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    titles = [t.title for t in library.tracks(artist="Alpha", album="Debut")]
    assert titles == ["Song A", "Song B"]


def test_search_requires_every_word(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    assert [t.title for t in library.tracks(search="beta encore")] == ["Encore"]
    assert library.tracks(search="alpha encore") == []


def test_albums_by_artist(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    assert library.albums("Beta") == [("Beta", "Live", None)]
    assert len(library.albums()) == 2


def test_play_counts_and_ratings_survive_a_retag(tmp_path):
    library, music, _ = _library_with_files(tmp_path)
    track = library.tracks(search="Encore")[0]
    library.record_play(track.id, when=1000.0)
    library.set_rating(track.id, 9)  # clamped to 5

    retagged = dict(TAGS, **{"b1.flac": ("Beta", "Live", "Encore (Remastered)", 1)})
    path = music / "b1.flac"
    path.write_bytes(b"xy")  # size change → rescanned

    result = library.scan([str(music)], read=_fake_reader(retagged))
    updated = library.track(track.id)

    assert result.updated == 1
    assert updated.title == "Encore (Remastered)"
    assert updated.play_count == 1
    assert updated.rating == 5
    assert [t.id for t in library.most_played()] == [track.id]


def test_remove_folder_forgets_its_tracks(tmp_path):
    library, music, _ = _library_with_files(tmp_path)
    assert library.remove_folder(str(music)) == 3
    assert library.track_count() == 0
