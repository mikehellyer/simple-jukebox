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


def test_app_log_brackets_the_run(tmp_path):
    import simple_jukebox.core.diagnostics as diagnostics

    path = diagnostics.enable_app_log("9.9.9", tmp_path, redirect_stderr=False)
    diagnostics.log_event("event loop finished (code 0)")
    diagnostics._log_exit()
    text = path.read_text()
    assert "started Simple-Jukebox 9.9.9" in text
    assert text.rstrip().endswith("exited normally")


def test_oversized_app_log_is_rotated(tmp_path, monkeypatch):
    import simple_jukebox.core.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "APP_LOG_MAX_BYTES", 10)
    (tmp_path / "app.log").write_text("x" * 100)
    diagnostics.enable_app_log("1.0", tmp_path, redirect_stderr=False)
    assert (tmp_path / "app.previous.log").read_text() == "x" * 100
    assert "x" * 100 not in (tmp_path / "app.log").read_text()
