"""Update checking against a GitHub Releases feed.

Never raises: a network hiccup or unreachable GitHub should not disturb normal
app use. Callers just get None back and can log it if they want.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Optional

from simple_jukebox.core.http import SSL_CONTEXT

API_TIMEOUT_SECONDS = 4


@dataclass
class UpdateInfo:
    version: str
    url: str
    notes: str
    assets: list[tuple[str, str]]  # (filename, browser_download_url)


def _parse_version(tag: str) -> tuple[int, ...]:
    cleaned = tag.lstrip("vV")
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(remote_tag: str, current_version: str) -> bool:
    return _parse_version(remote_tag) > _parse_version(current_version)


def check_for_update(current_version: str, owner: str, repo: str) -> Optional[UpdateInfo]:
    """Return UpdateInfo if a newer release is published, else None.

    Swallows every failure mode (offline, rate-limited, malformed response)
    so it is always safe to call from app startup.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    tag = payload.get("tag_name")
    if not tag or not is_newer(tag, current_version):
        return None

    assets = [
        (asset["name"], asset["browser_download_url"])
        for asset in payload.get("assets", [])
        if "name" in asset and "browser_download_url" in asset
    ]
    return UpdateInfo(
        version=tag,
        url=payload.get("html_url", ""),
        notes=payload.get("body", ""),
        assets=assets,
    )
