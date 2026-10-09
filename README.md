# Simple-Jukebox

A simple music library manager and player for Linux, macOS, and Windows,
in the vein of iTunes and Strawberry.

Created by [Mike Hellyer](https://github.com/mikehellyer), built with
[Claude Code](https://claude.com/claude-code).

## Status

Point it at your music folders (**File ▸ Add Music Folder…**) and it builds
a library from the files' tags (MP3, FLAC, Ogg/Opus, M4A, WAV, …), falling
back to the `Artist/Album/NN Title.ext` folder layout for untagged files.
Browse iTunes-style by artist and album above the track list, or use
**Recently Added** / **Most Played**, and search across everything.
Double-click a track to play from there; shuffle, repeat (all/one), volume,
play counts, star ratings, and embedded or folder (`cover.jpg`) album art
are all supported. Rescans are incremental, so re-opening a big library is
quick.

**Playlists** live in the sidebar: make one with **File ▸ New Playlist…**
(or "Add to Playlist ▸ New Playlist…" on selected songs), then drag songs
onto it. Inside a playlist, drag rows to reorder them and press Delete to
remove them. Right-click a playlist to play, rename, or delete it.

**Up Next** (the panel on the right, **View ▸ Show Up Next** / Ctrl+U)
shows what's playing and what follows. Right-click songs and choose **Play
Next** or **Add to Up Next**, or drag them into the panel. Within the
panel, drag to reorder, double-click to jump ahead, and press Delete to
remove a song.

**Sync to Device** (**File ▸ Sync to Device…**, Ctrl+Shift+S) copies music
onto a portable player — a Sony Walkman, a phone, or any SD card or USB
stick. Give the device a name and pick its Music folder (on Linux a player
plugged in by USB shows up under **Detected** once it's unlocked and opened in
the file manager; on any OS you can use its microSD card in a card reader).
Choose the entire library or particular playlists and artists; songs go into
`Artist/Album` folders and playlists become `.m3u` files the player can read.
Syncs are incremental, and "remove songs no longer selected" only ever deletes
files Simple-Jukebox itself copied — it keeps a list of those on the device.
Each device's choices are remembered for next time.

## Development setup

```bash
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -e .
```

Run the app:

```bash
python -m simple_jukebox.app
```

Run the tests (core logic only — no display required):

```bash
pytest
```

## Project layout

- `src/simple_jukebox/core/` — tag reading, the SQLite library (tracks
  and playlists), the Up Next play queue, settings, and the update checker; plain Python, no Qt dependency,
  covered by `tests/`.
- `src/simple_jukebox/gui/` — the PySide6 window and widgets.
- `src/simple_jukebox/app.py` — entry point.
- `packaging/` — the icon generator, PyInstaller spec, and per-OS installer
  scripts (see Releasing, below).

## Updates

On startup the app checks the repo's latest GitHub Release in the
background and, if a newer version is published, shows a banner with an
**Update** button — it never downloads or installs anything until that's
clicked. Unreachable GitHub just means no banner; it never blocks or
errors the app.

## Releasing

1. Bump `__version__` in `src/simple_jukebox/__init__.py` (and `pyproject.toml`).
2. Commit, then tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The [release workflow](.github/workflows/release.yml) builds all three
   platforms and attaches the installers to a GitHub Release for that tag:
   - **Windows**: PyInstaller → Inno Setup → `Simple-Jukebox-X.Y.Z-Windows-Setup.exe`
   - **macOS**: PyInstaller → `.app` → `Simple-Jukebox-X.Y.Z-macOS.dmg`
   - **Linux**: PyInstaller → `simple-jukebox_X.Y.Z_amd64.deb`

To build one platform's installer locally (must run on that OS):

```bash
pip install -r requirements-dev.txt
pyinstaller --noconfirm packaging/pyinstaller/simple-jukebox.spec
# then, per platform:
bash packaging/macos/build-dmg.sh      # macOS
bash packaging/linux/build-deb.sh      # Linux
iscc packaging/windows/installer.iss   # Windows (Inno Setup installed)
```
