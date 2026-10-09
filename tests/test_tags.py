import wave

from simple_jukebox.core.tags import _leading_int, is_audio_file, read_track


def _write_silent_wav(path, seconds=1):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 8000 * seconds)


def test_untagged_file_falls_back_to_folder_layout(tmp_path):
    album_dir = tmp_path / "Some Band" / "First Record"
    album_dir.mkdir(parents=True)
    path = album_dir / "03 - Opening Song.wav"
    _write_silent_wav(path, seconds=2)

    info = read_track(path)

    assert info is not None
    assert info.title == "Opening Song"
    assert info.track_number == 3
    assert info.album == "First Record"
    assert info.artist == "Some Band"
    assert info.album_artist == "Some Band"
    assert round(info.duration) == 2


def test_non_audio_file_reads_as_none(tmp_path):
    path = tmp_path / "notes.mp3"
    path.write_text("not really audio")
    assert read_track(path) is None


def test_is_audio_file_by_extension(tmp_path):
    assert is_audio_file(tmp_path / "a.FLAC")
    assert not is_audio_file(tmp_path / "cover.jpg")


def test_leading_int_handles_common_tag_shapes():
    assert _leading_int("3/12") == 3
    assert _leading_int("2004-05-01") == 2004
    assert _leading_int("") is None

