from simple_jukebox.core.text import format_duration


def test_format_duration_minutes_and_hours():
    assert format_duration(0) == "0:00"
    assert format_duration(65.4) == "1:05"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(-3) == "0:00"
