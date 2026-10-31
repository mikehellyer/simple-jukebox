from pathlib import Path

from simple_jukebox.core.device_identity import (
    IDENTITY_NAME,
    PODCASTS_BESIDE_MUSIC,
    PODCASTS_CUSTOM,
    PODCASTS_IN_MUSIC,
    DeviceCheck,
    check_device,
    default_podcast_location,
    new_device_id,
    podcast_folder,
    read_identity,
    write_identity,
)


def test_identity_round_trip(tmp_path):
    device_id = new_device_id()
    write_identity(tmp_path, device_id, "Walkman NW-A55")
    identity = read_identity(tmp_path)
    assert identity["id"] == device_id and identity["name"] == "Walkman NW-A55"
    assert new_device_id() != device_id


def test_corrupt_or_missing_identity_reads_as_none(tmp_path):
    assert read_identity(tmp_path) is None
    (tmp_path / IDENTITY_NAME).write_text("{oops")
    assert read_identity(tmp_path) is None
    (tmp_path / IDENTITY_NAME).write_text('{"id": ""}')
    assert read_identity(tmp_path) is None


def test_check_device_outcomes(tmp_path):
    a55 = tmp_path / "WALKMAN" / "MUSIC"
    a55.mkdir(parents=True)
    # First sync of a new profile onto an unmarked device: fine to claim.
    assert check_device(a55, "").status == DeviceCheck.NEW
    a55_id = new_device_id()
    write_identity(a55, a55_id, "Walkman NW-A55")
    assert check_device(a55, a55_id).status == DeviceCheck.MATCH

    # The NW-A306 profile pointed at the same mount point finds the A55.
    result = check_device(a55, new_device_id())
    assert result.status == DeviceCheck.OTHER and result.found_name == "Walkman NW-A55"
    # A brand-new profile can't claim a device that's already someone else's.
    assert check_device(a55, "").status == DeviceCheck.OTHER

    # A known device's folder that has lost its identity (reset, new card).
    blank = tmp_path / "card" / "MUSIC"
    blank.mkdir(parents=True)
    assert check_device(blank, a55_id).status == DeviceCheck.UNMARKED
    # Not plugged in.
    assert check_device(tmp_path / "nowhere", a55_id).status == DeviceCheck.NOT_FOUND


def _device(tmp_path: Path, android: bool) -> Path:
    root = tmp_path / ("Internal shared storage" if android else "WALKMAN")
    (root / "Music").mkdir(parents=True)
    if android:
        (root / "Android").mkdir()
    return root / "Music"


def test_podcast_location_defaults_by_player_type(tmp_path):
    a306 = _device(tmp_path / "a306", android=True)
    a55 = _device(tmp_path / "a55", android=False)
    assert default_podcast_location(str(a306)) == PODCASTS_BESIDE_MUSIC
    assert default_podcast_location(str(a55)) == PODCASTS_IN_MUSIC
    assert default_podcast_location("") == PODCASTS_IN_MUSIC


def test_podcast_folder_for_each_location(tmp_path):
    music = _device(tmp_path, android=True)
    assert podcast_folder(str(music), PODCASTS_BESIDE_MUSIC) == str(music.parent / "Podcasts")
    (music.parent / "PODCASTS").mkdir()
    assert podcast_folder(str(music), PODCASTS_BESIDE_MUSIC) == str(music.parent / "PODCASTS")
    assert podcast_folder(str(music), PODCASTS_IN_MUSIC) == str(music / "Podcasts")
    assert podcast_folder(str(music), PODCASTS_CUSTOM, "/x/y") == "/x/y"
    assert podcast_folder("", PODCASTS_IN_MUSIC) == ""
