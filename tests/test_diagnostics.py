import faulthandler

from simple_jukebox.core.diagnostics import enable_crash_log, note_current_track


def test_crash_log_is_created_and_enabled(tmp_path):
    path = enable_crash_log(tmp_path / "logs")
    try:
        assert path is not None and path.exists()
        assert faulthandler.is_enabled()
    finally:
        faulthandler.disable()


def test_note_current_track_records_the_path(tmp_path):
    note_current_track("/music/a.mp3", tmp_path)
    note_current_track("/music/b.flac", tmp_path)
    assert (tmp_path / "last_track.txt").read_text().rstrip().endswith("/music/b.flac")


def test_note_current_track_never_raises(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    note_current_track("/music/a.mp3", blocker)
