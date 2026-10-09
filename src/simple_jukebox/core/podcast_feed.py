"""Reading a podcast's RSS feed: the show's details and its episodes.

Fuller than Simple-Streamer's feed reader (which only needs something to
stream): subscribing needs each episode's stable id (its <guid>), date,
file size and description, and the show's artwork, so episodes can be
tracked, downloaded and tagged.
"""
from __future__ import annotations

import email.utils
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from simple_jukebox.core.http import SSL_CONTEXT

FEED_TIMEOUT_SECONDS = 15
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_IT = f"{{{ITUNES_NS}}}"
_TAG = re.compile(r"<[^>]+>")


class FeedError(Exception):
    """The feed couldn't be fetched or isn't a podcast feed."""


@dataclass
class FeedEpisode:
    guid: str
    title: str
    audio_url: str
    published: float = 0.0  # Unix time; 0 if the feed gave no usable date
    duration: int = 0  # seconds; 0 if unknown
    file_size: int = 0  # bytes, from the enclosure; 0 if unknown
    mime_type: str = ""
    description: str = ""


@dataclass
class Feed:
    title: str
    author: str = ""
    description: str = ""
    image_url: str = ""
    link: str = ""
    episodes: list[FeedEpisode] = field(default_factory=list)  # newest first


def fetch_feed(url: str) -> Feed:
    request = urllib.request.Request(url, headers={"User-Agent": "Simple-Jukebox/1 (+podcast manager)"})
    try:
        with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
            data = response.read()
    except (OSError, ValueError) as error:
        raise FeedError(f"Couldn't download the feed: {error}") from error
    return parse_feed(data)


def parse_feed(data: bytes) -> Feed:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise FeedError("That isn't a podcast feed (not valid RSS/XML).") from error
    channel = root.find("channel")
    if channel is None:
        raise FeedError("That isn't a podcast feed (no <channel>).")

    image = channel.find(f"{_IT}image")
    image_url = image.get("href", "") if image is not None else ""
    if not image_url:
        image_url = channel.findtext("image/url", default="")

    feed = Feed(
        title=_text(channel, "title") or "Untitled podcast",
        author=_text(channel, f"{_IT}author") or _text(channel, "managingEditor"),
        description=_plain(_text(channel, f"{_IT}summary") or _text(channel, "description")),
        image_url=image_url.strip(),
        link=_text(channel, "link"),
    )
    for item in channel.findall("item"):
        episode = _parse_item(item)
        if episode is not None:
            feed.episodes.append(episode)
    feed.episodes.sort(key=lambda e: e.published, reverse=True)
    return feed


def _parse_item(item) -> Optional[FeedEpisode]:
    enclosure = item.find("enclosure")
    audio_url = (enclosure.get("url") or "").strip() if enclosure is not None else ""
    if not audio_url:
        return None  # nothing to play or download
    try:
        size = int(enclosure.get("length") or 0)
    except ValueError:
        size = 0
    return FeedEpisode(
        # Feeds without a <guid> are common enough; the audio URL is the
        # next most stable identifier.
        guid=_text(item, "guid") or audio_url,
        title=_text(item, "title") or "Untitled episode",
        audio_url=audio_url,
        published=parse_date(_text(item, "pubDate")),
        duration=parse_duration(_text(item, f"{_IT}duration")),
        file_size=max(0, size),
        mime_type=(enclosure.get("type") or "").strip(),
        description=_plain(_text(item, f"{_IT}summary") or _text(item, "description")),
    )


def _text(element, path: str) -> str:
    return (element.findtext(path) or "").strip()


def _plain(html: str) -> str:
    """Show notes are often HTML — keep just the words."""
    import html as html_module

    text = _TAG.sub(" ", html or "")
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def parse_date(raw: str) -> float:
    """RFC 822 pubDate → Unix time; 0 if missing or unparseable."""
    if not raw:
        return 0.0
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        import datetime

        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def parse_duration(raw: str) -> int:
    """itunes:duration is either plain seconds ("2823") or [HH:]MM:SS."""
    if not raw:
        return 0
    parts = raw.strip().split(":")
    try:
        numbers = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for number in numbers[-3:]:
        seconds = seconds * 60 + number
    return max(0, seconds)
