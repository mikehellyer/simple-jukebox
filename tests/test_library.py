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


def test_playlists_keep_order_and_duplicates(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    ids = {t.title: t.id for t in library.tracks()}
    playlist = library.create_playlist("  Road Trip  ")
    library.add_to_playlist(playlist, [ids["Encore"], ids["Song A"], ids["Encore"]])

    assert [p.name for p in library.playlists()] == ["Road Trip"]
    assert library.playlists()[0].track_count == 3
    assert [t.title for t in library.playlist_tracks(playlist)] == ["Encore", "Song A", "Encore"]

    library.remove_from_playlist(playlist, [0])
    assert [t.title for t in library.playlist_tracks(playlist)] == ["Song A", "Encore"]


def test_move_in_playlist_behaves_like_drag_and_drop(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    ids = {t.title: t.id for t in library.tracks()}
    playlist = library.create_playlist("Mix")
    library.add_to_playlist(playlist, [ids["Song A"], ids["Song B"], ids["Encore"]])

    library.move_in_playlist(playlist, [0], 3)  # drag first to the end
    assert [t.title for t in library.playlist_tracks(playlist)] == ["Song B", "Encore", "Song A"]
    library.move_in_playlist(playlist, [1, 2], 0)  # drag last two to the top
    assert [t.title for t in library.playlist_tracks(playlist)] == ["Encore", "Song A", "Song B"]


def test_deleted_files_drop_out_of_playlists(tmp_path):
    library, music, _ = _library_with_files(tmp_path)
    ids = {t.title: t.id for t in library.tracks()}
    playlist = library.create_playlist("Mix")
    library.add_to_playlist(playlist, [ids["Encore"], ids["Song A"]])

    (music / "b1.flac").unlink()
    library.scan([str(music)], read=_fake_reader(TAGS))

    assert [t.title for t in library.playlist_tracks(playlist)] == ["Song A"]


def test_rename_and_delete_playlist(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    playlist = library.create_playlist("Old")
    library.add_to_playlist(playlist, [library.tracks()[0].id])
    library.rename_playlist(playlist, "New")
    library.rename_playlist(playlist, "   ")  # ignored
    assert library.playlists()[0].name == "New"
    library.delete_playlist(playlist)
    assert library.playlists() == []
    assert library.track_count() == 3


def test_tracks_by_ids_skips_unknown_ids(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    ids = [t.id for t in library.tracks()]
    found = library.tracks_by_ids(ids + [9999])
    assert sorted(found) == sorted(ids)


def test_already_in_playlist_finds_existing_and_repeated_songs():
    from simple_jukebox.core.library import already_in_playlist

    assert already_in_playlist([1, 2], [3, 2, 4, 3, 1]) == [1, 3, 4]
    assert already_in_playlist([], [5, 6]) == []


def test_album_summaries_group_and_search(tmp_path):
    library, _, _ = _library_with_files(tmp_path)
    albums = library.album_summaries()
    assert [(a.album_artist, a.album, a.track_count) for a in albums] == [
        ("Alpha", "Debut", 2),
        ("Beta", "Live", 1),
    ]
    assert albums[0].cover_path.endswith("a2.mp3")  # Song A is track 1
    assert [a.album for a in library.album_summaries(search="encore")] == ["Live"]
    assert [t.title for t in library.album_tracks("Alpha", "Debut")] == ["Song A", "Song B"]
