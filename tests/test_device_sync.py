from pathlib import Path

from simple_jukebox.core.device_sync import (
    MANIFEST_NAME,
    device_path_for,
    m3u_text,
    plan_sync,
    read_manifest,
    run_sync,
    safe_name,
)
from simple_jukebox.core.library import Track


def _track(tmp_path, track_id, title, artist="Band", album="Record", number=1, size=10, ext=".flac", disc=None):
    source = tmp_path / "library" / f"{track_id}{ext}"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"a" * size)
    return Track(
        id=track_id, path=str(source), title=title, artist=artist, album=album, album_artist=artist,
        genre="", year=None, track_number=number, disc_number=disc, duration=200.4,
        date_added=0, play_count=0, last_played=None, rating=0,
    )


def test_safe_name_strips_characters_sd_cards_reject():
    assert safe_name('AC/DC: "Live"?') == "AC_DC_ _Live__"
    assert safe_name("Trailing dots...") == "Trailing dots"
    assert safe_name("   ") == "Unknown"


def test_device_path_layout(tmp_path):
    track = _track(tmp_path, 1, "Dream Police", artist="Cheap Trick", album="Dream Police", number=1)
    assert device_path_for(track) == "Cheap Trick/Dream Police/01 Dream Police.flac"
    disc_two = _track(tmp_path, 2, "Bonus", number=3, disc=2)
    assert device_path_for(disc_two).endswith("/2-03 Bonus.flac")


def test_first_sync_copies_everything_and_writes_playlist(tmp_path):
    dest = tmp_path / "walkman" / "Music"
    dest.mkdir(parents=True)
    a, b = _track(tmp_path, 1, "A", number=1), _track(tmp_path, 2, "B", number=2)

    plan = plan_sync(dest, [a, b], playlists={"Road/Trip": [b, a]})
    assert len(plan.copies) == 2 and plan.bytes_to_copy == 20
    result = run_sync(plan)

    assert result.copied == 2 and not result.failed
    assert (dest / "Band/Record/01 A.flac").read_bytes() == b"a" * 10
    playlist = (dest / "Road_Trip.m3u").read_text()
    assert playlist.splitlines()[0] == "#EXTM3U"
    assert playlist.splitlines()[2] == "Band/Record/02 B.flac"
    assert read_manifest(dest) == {"Band/Record/01 A.flac", "Band/Record/02 B.flac", "Road_Trip.m3u"}


def test_second_sync_skips_unchanged_and_removes_deselected(tmp_path):
    dest = tmp_path / "dev"
    dest.mkdir()
    a, b = _track(tmp_path, 1, "A", number=1), _track(tmp_path, 2, "B", number=2)
    run_sync(plan_sync(dest, [a, b]))

    plan = plan_sync(dest, [a])
    assert plan.copies == [] and plan.unchanged == 1
    assert plan.deletes == ["Band/Record/02 B.flac"]
    run_sync(plan)
    assert not (dest / "Band/Record/02 B.flac").exists()
    assert read_manifest(dest) == {"Band/Record/01 A.flac"}

    run_sync(plan_sync(dest, []))
    assert not (dest / "Band").exists()  # emptied folders are tidied away


def test_never_touches_files_it_did_not_put_there(tmp_path):
    dest = tmp_path / "dev"
    own = dest / "Band/Record/01 A.flac"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"users own rip, different size")
    other = dest / "Podcasts/episode.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    a = _track(tmp_path, 1, "A", number=1)

    plan = plan_sync(dest, [a])
    assert plan.copies == [] and plan.unchanged == 1  # not overwritten
    run_sync(plan)
    run_sync(plan_sync(dest, []))  # deselect everything
    assert own.read_bytes() == b"users own rip, different size"
    assert other.exists()


def test_keep_unselected_songs_when_asked(tmp_path):
    dest = tmp_path / "dev"
    dest.mkdir()
    a = _track(tmp_path, 1, "A")
    run_sync(plan_sync(dest, [a]))
    plan = plan_sync(dest, [], remove_unselected=False)
    assert plan.deletes == []
    run_sync(plan)
    assert read_manifest(dest) == {"Band/Record/01 A.flac"}  # still ours, for a later cleanup


def test_changed_source_is_recopied(tmp_path):
    dest = tmp_path / "dev"
    dest.mkdir()
    a = _track(tmp_path, 1, "A")
    run_sync(plan_sync(dest, [a]))
    Path(a.path).write_bytes(b"retagged, now longer")
    plan = plan_sync(dest, [a])
    assert [rel for _, rel in plan.copies] == ["Band/Record/01 A.flac"]


def test_cancel_leaves_no_partial_file_and_a_truthful_manifest(tmp_path):
    dest = tmp_path / "dev"
    dest.mkdir()
    tracks = [_track(tmp_path, i, f"T{i}", number=i) for i in range(1, 4)]
    plan = plan_sync(dest, tracks)
    calls = []

    def progress(done, total, label):
        calls.append(label)

    result = run_sync(plan, progress=progress, cancelled=lambda: len(calls) >= 2)
    assert result.cancelled
    assert result.copied == 1
    assert read_manifest(dest) == {"Band/Record/01 T1.flac"}
    assert not (dest / "Band/Record/02 T2.flac").exists()


def test_missing_source_files_are_skipped(tmp_path):
    dest = tmp_path / "dev"
    dest.mkdir()
    a = _track(tmp_path, 1, "A")
    Path(a.path).unlink()
    assert plan_sync(dest, [a]).copies == []


def test_playlist_lists_only_songs_on_the_device(tmp_path):
    a, b = _track(tmp_path, 1, "A", number=1), _track(tmp_path, 2, "B", number=2)
    text = m3u_text([a])
    assert "#EXTINF:200,Band - A" in text
    plan = plan_sync(tmp_path / "nowhere", [a], playlists={"Mix": [a, b]})
    assert "02 B.flac" not in plan.playlists["Mix.m3u"]


def test_corrupt_manifest_is_treated_as_empty(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{not json")
    assert read_manifest(tmp_path) == set()
