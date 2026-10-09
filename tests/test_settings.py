from simple_jukebox.core.settings import SettingsStore


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.add_music_folder(str(tmp_path / "Music"))
    store.set_volume(150)
    store.set_shuffle(True)
    store.set_repeat("all")
    store.set_repeat("bogus")

    reloaded = SettingsStore(path)
    assert reloaded.music_folders == [str((tmp_path / "Music").resolve())]
    assert reloaded.volume == 100
    assert reloaded.shuffle is True
    assert reloaded.repeat == "all"


def test_nested_or_duplicate_folders_are_rejected(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.add_music_folder(str(tmp_path / "Music"))
    assert not store.add_music_folder(str(tmp_path / "Music"))
    assert not store.add_music_folder(str(tmp_path / "Music" / "Rock"))


def test_corrupt_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    store = SettingsStore(path)
    assert store.music_folders == []
    assert store.volume == 80


def test_device_profiles_round_trip_and_rename(tmp_path):
    from simple_jukebox.core.settings import DeviceProfile

    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save_device(DeviceProfile(name="Walkman", path="/media/sd/Music", playlist_ids=[3], artists=["Kiss"]))
    store.save_device(DeviceProfile(name="Callback", path="/run/x", whole_library=True))
    store.save_device(DeviceProfile(name="Walkman NW-A306", path="/media/sd/Music"), old_name="Walkman")

    reloaded = SettingsStore(path)
    assert [d.name for d in reloaded.devices] == ["Callback", "Walkman NW-A306"]
    assert reloaded.devices[0].whole_library is True
    reloaded.remove_device("Callback")
    assert [d.name for d in SettingsStore(path).devices] == ["Walkman NW-A306"]


def test_bad_device_entries_are_dropped(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"devices": [{"name": "ok", "path": "/x"}, {"name": 5}, "junk"]}')
    assert [d.name for d in SettingsStore(path).devices] == ["ok"]
