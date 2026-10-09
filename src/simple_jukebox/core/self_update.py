"""Downloading and launching an update, once the user has asked for it.

Simple-Jukebox never does this on its own — check_for_update() only ever
notifies. Everything here only runs after an explicit "Update" click.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from simple_jukebox.core.http import SSL_CONTEXT

DOWNLOAD_TIMEOUT_SECONDS = 30


def _installer_suffix() -> str:
    if sys.platform == "win32":
        return ".exe"
    if sys.platform == "darwin":
        return ".dmg"
    return ".deb"


def find_asset_for_this_platform(assets: list[tuple[str, str]]) -> Optional[str]:
    """Return the download URL of the release asset built for this OS, if any."""
    suffix = _installer_suffix()
    for name, download_url in assets:
        if name.lower().endswith(suffix):
            return download_url
    return None


def download_asset(url: str) -> Optional[Path]:
    """Download a release asset to a fresh temp directory. None on failure."""
    filename = url.rsplit("/", 1)[-1] or "simple-jukebox-update"
    dest_dir = Path(tempfile.mkdtemp(prefix="simple-jukebox-update-"))
    dest_path = dest_dir / filename

    request = urllib.request.Request(url, headers={"User-Agent": "Simple-Jukebox/1"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
            with open(dest_path, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
    except OSError:
        return None
    return dest_path


def launch_installer(path: Path) -> Optional[subprocess.Popen]:
    """Hand the downloaded installer off to the OS's normal action for it
    (runs the .exe, mounts the .dmg, installs the .deb). Raises OSError
    if nothing could be launched at all — the caller decides how to tell
    the user.

    Returns the spawned process where there is one, so a caller on Linux
    can wait for it to finish before quitting — quitting immediately
    there can kill the pkexec authentication prompt before it's
    answered, since the desktop session ties a launched app's child
    processes to its own lifetime. Windows has no equivalent handle for
    os.startfile, hence None there even on success.
    """
    path = str(path)
    if sys.platform == "win32":
        import os

        os.startfile(path)
        return None
    elif sys.platform == "darwin":
        process = subprocess.Popen(["open", path])
        # `open` on a .dmg mounts it and opens a Finder window for it,
        # but that window doesn't reliably become frontmost on its own —
        # Finder can stay in the background, which (right before this
        # app quits a moment later) looks exactly like clicking Update
        # did nothing. Nudging Finder forward is harmless if it's
        # already there, and costs nothing if this fails.
        try:
            subprocess.Popen(["open", "-a", "Finder"])
        except OSError:
            pass
        return process
    else:
        # Handing a local .deb of an already-installed package to a
        # desktop "Software" GUI via xdg-open is unreliable across
        # distros: several (including GNOME Software / Pop!_Shop) show
        # an "Uninstall" action instead of "Install"/"Reinstall" for a
        # package already on the system, regardless of the downloaded
        # file's version — clicking it just removes the current install
        # and does nothing with the new file, forcing a separate manual
        # reinstall (and a second password prompt). Installing directly
        # via apt (through pkexec for a graphical privilege prompt)
        # upgrades in place with a single prompt instead.
        pkexec_path = shutil.which("pkexec")
        apt_path = shutil.which("apt")
        if pkexec_path and apt_path:
            # pkexec resolves the command it's given on its own, using a
            # restricted environment rather than the invoking shell's
            # $PATH — a bare "apt" can fail there (exit 127, "command
            # not found") even though `apt` works fine normally. Passing
            # the already-resolved absolute path sidesteps that.
            #
            # stderr is piped (not stdout — apt's stdout can be large,
            # and nothing reads it while this process is still running,
            # risking a full-pipe deadlock; stderr from a failed pkexec/
            # apt is always short) so a failure can show pkexec/apt's
            # actual error text instead of just a bare exit code — an
            # exit code alone hasn't been enough to diagnose this so far.
            return subprocess.Popen(
                [pkexec_path, apt_path, "install", "-y", path],
                stderr=subprocess.PIPE,
                text=True,
            )
        return subprocess.Popen(["xdg-open", path])
