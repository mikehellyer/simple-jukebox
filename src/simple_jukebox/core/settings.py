"""Persisted user preferences: which folders make up the library, plus
playback and layout state worth remembering between runs (volume,
shuffle, repeat, whether the Up Next panel is showing).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from platformdirs import user_config_dir

REPEAT_MODES = ("off", "all", "one")


@dataclass
class DeviceProfile:
    """A player to sync to: where it mounts and what goes on it."""

    name: str
    path: str
    whole_library: bool = False
    playlist_ids: list[int] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    copy_playlists: bool = True
    remove_unselected: bool = True

    @classmethod
    def from_dict(cls, data) -> Optional["DeviceProfile"]:
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("path"), str):
            return None
        return cls(
            name=data["name"],
            path=data["path"],
            whole_library=bool(data.get("whole_library", False)),
            playlist_ids=[i for i in data.get("playlist_ids", []) if isinstance(i, int)],
            artists=[a for a in data.get("artists", []) if isinstance(a, str)],
            copy_playlists=bool(data.get("copy_playlists", True)),
            remove_unselected=bool(data.get("remove_unselected", True)),
        )


@dataclass
class Settings:
    music_folders: list[str] = field(default_factory=list)
    volume: int = 80
    shuffle: bool = False
    repeat: str = "off"
    show_up_next: bool = True
    devices: list[DeviceProfile] = field(default_factory=list)


class SettingsStore:
    def __init__(self, config_path: Optional[Path] = None):
        self._config_path = config_path or self._default_config_path()
        self._settings = Settings()
        self.load()

    @staticmethod
    def _default_config_path() -> Path:
        return Path(user_config_dir("Simple-Jukebox")) / "settings.json"

    @property
    def music_folders(self) -> list[str]:
        return list(self._settings.music_folders)

    def add_music_folder(self, folder: str) -> bool:
        """Add a folder; False if it (or a folder containing it) is already there."""
        new = Path(folder).resolve()
        for existing in self._settings.music_folders:
            existing_path = Path(existing).resolve()
            if new == existing_path or existing_path in new.parents:
                return False
        self._settings.music_folders.append(str(new))
        self.save()
        return True

    def remove_music_folder(self, folder: str) -> None:
        self._settings.music_folders = [f for f in self._settings.music_folders if f != folder]
        self.save()

    @property
    def volume(self) -> int:
        return self._settings.volume

    def set_volume(self, volume: int) -> None:
        self._settings.volume = max(0, min(100, int(volume)))
        self.save()

    @property
    def shuffle(self) -> bool:
        return self._settings.shuffle

    def set_shuffle(self, shuffle: bool) -> None:
        self._settings.shuffle = shuffle
        self.save()

    @property
    def repeat(self) -> str:
        return self._settings.repeat

    def set_repeat(self, repeat: str) -> None:
        if repeat in REPEAT_MODES:
            self._settings.repeat = repeat
            self.save()

    @property
    def show_up_next(self) -> bool:
        return self._settings.show_up_next

    def set_show_up_next(self, show: bool) -> None:
        self._settings.show_up_next = show
        self.save()

    @property
    def devices(self) -> list[DeviceProfile]:
        return list(self._settings.devices)

    def save_device(self, profile: DeviceProfile, old_name: Optional[str] = None) -> None:
        """Add a device, or replace the one called old_name (or profile.name)."""
        key = old_name if old_name is not None else profile.name
        devices = [d for d in self._settings.devices if d.name != key and d.name != profile.name]
        devices.append(profile)
        self._settings.devices = sorted(devices, key=lambda d: d.name.lower())
        self.save()

    def remove_device(self, name: str) -> None:
        self._settings.devices = [d for d in self._settings.devices if d.name != name]
        self.save()

    def load(self) -> None:
        if not self._config_path.exists():
            return
        try:
            data = json.loads(self._config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        folders = data.get("music_folders")
        if isinstance(folders, list):
            self._settings.music_folders = [f for f in folders if isinstance(f, str)]
        if isinstance(data.get("volume"), int):
            self._settings.volume = max(0, min(100, data["volume"]))
        if isinstance(data.get("shuffle"), bool):
            self._settings.shuffle = data["shuffle"]
        if isinstance(data.get("show_up_next"), bool):
            self._settings.show_up_next = data["show_up_next"]
        if isinstance(data.get("devices"), list):
            self._settings.devices = [
                profile for profile in map(DeviceProfile.from_dict, data["devices"]) if profile is not None
            ]
        if data.get("repeat") in REPEAT_MODES:
            self._settings.repeat = data["repeat"]

    def save(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(asdict(self._settings), indent=2))
