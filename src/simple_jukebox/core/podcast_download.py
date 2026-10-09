"""Downloading podcast episodes and tagging them as podcasts.

Files go into the Podcasts folder as <Show>/<YYYY-MM-DD> <Episode>.<ext>.
Each download is tagged the way players recognise a podcast rather than
music — the show as album, genre "Podcast", and the format's own podcast
markers (ID3's PCST "podcast" flag and feed/episode ids for MP3; iTunes'
pcst flag and "podcast" media kind for M4A) — so a Walkman or phone files
them under Podcasts.
"""
from __future__ import annotations

import datetime
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from platformdirs import user_music_dir

from simple_jukebox.core.device_sync import safe_name
from simple_jukebox.core.http import SSL_CONTEXT
from simple_jukebox.core.podcasts import Episode

DOWNLOAD_TIMEOUT_SECONDS = 30
CHUNK_BYTES = 256 * 1024
PODCAST_GENRE = "Podcast"
_MAX_DESCRIPTION = 2000  # some show notes run to pages; tags don't need it all

_EXTENSION_FOR_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/x-m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
}
_KNOWN_EXTENSIONS = set(_EXTENSION_FOR_TYPE.values()) | {".oga", ".wav"}


class DownloadCancelled(Exception):
    pass


def default_podcasts_folder() -> Path:
    # Beside (not inside) the Music folder, so library scans of ~/Music
    # never pick episodes up as songs.
    return Path(user_music_dir()).parent / "Podcasts"


def episode_extension(episode: Episode) -> str:
    path = urllib.parse.urlparse(episode.audio_url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in _KNOWN_EXTENSIONS:
        return ext
    return _EXTENSION_FOR_TYPE.get(episode.mime_type.lower().split(";")[0].strip(), ".mp3")


def episode_date(episode: Episode) -> str:
    if not episode.published:
        return ""
    return datetime.datetime.fromtimestamp(episode.published, datetime.timezone.utc).strftime("%Y-%m-%d")


def episode_relative_path(podcast_title: str, episode: Episode) -> str:
    """Show/YYYY-MM-DD Title.ext — the same layout on disk and on devices."""
    date = episode_date(episode)
    name = safe_name(f"{date} {episode.title}".strip(), "Episode")
    return f"{safe_name(podcast_title, 'Podcast')}/{name}{episode_extension(episode)}"


def download_episode(
    episode: Episode,
    target: Path,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download to target (via a .part file, so a half-finished download
    never looks complete). Raises OSError on failure, DownloadCancelled."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(episode.audio_url, headers={"User-Agent": "Simple-Jukebox/1 (+podcast manager)"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
            total = int(response.headers.get("Content-Length") or 0) or episode.file_size
            done = 0
            with open(partial, "wb") as out:
                while True:
                    if cancelled():
                        raise DownloadCancelled()
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        os.replace(partial, target)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return target


# --- tagging -------------------------------------------------------------------


@dataclass
class PodcastTags:
    title: str
    show: str
    author: str
    date: str  # YYYY-MM-DD, or ""
    description: str
    feed_url: str
    episode_url: str
    guid: str


def podcast_tags(show: str, author: str, feed_url: str, episode: Episode) -> PodcastTags:
    return PodcastTags(
        title=episode.title,
        show=show,
        author=author or show,
        date=episode_date(episode),
        description=(episode.description or "")[:_MAX_DESCRIPTION],
        feed_url=feed_url,
        episode_url=episode.audio_url,
        guid=episode.guid,
    )


def mp4_atoms(tags: PodcastTags) -> dict:
    """iTunes-style atoms: pcst + stik 21 are what mark an M4A as a podcast."""
    atoms = {
        "\xa9nam": [tags.title],
        "\xa9alb": [tags.show],
        "\xa9ART": [tags.author],
        "aART": [tags.show],
        "\xa9gen": [PODCAST_GENRE],
        "pcst": True,
        "stik": [21],  # media kind: Podcast
        "purl": [tags.episode_url],
        "egid": [tags.guid.encode("utf-8")],
        "catg": [PODCAST_GENRE],
    }
    if tags.date:
        atoms["\xa9day"] = [tags.date]
    if tags.description:
        atoms["desc"] = [tags.description[:255]]
        atoms["ldes"] = [tags.description]
    return atoms


def tag_as_podcast(path: Path, tags: PodcastTags) -> bool:
    """Write podcast tags into the file. Best effort: False if the format
    isn't taggable — the file still plays, and the device's Podcasts
    folder alone marks it as a podcast on Android."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            _tag_id3(path, tags)
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4

            audio = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()
            for key, value in mp4_atoms(tags).items():
                audio.tags[key] = value
            audio.save()
        elif ext in (".ogg", ".oga", ".opus", ".flac"):
            import mutagen

            audio = mutagen.File(str(path))
            if audio is None:
                return False
            if audio.tags is None:
                audio.add_tags()
            values = {
                "title": tags.title, "album": tags.show, "artist": tags.author, "albumartist": tags.show,
                "genre": PODCAST_GENRE, "date": tags.date, "comment": tags.description,
                "podcasturl": tags.feed_url, "podcast": "1",
            }
            for key, value in values.items():
                if value:
                    audio.tags[key] = [value]
            audio.save()
        else:
            return False
    except Exception:  # mutagen raises many format-specific errors
        return False
    return True


def _tag_id3(path: Path, tags: PodcastTags) -> None:
    from mutagen.id3 import COMM, ID3, PCST, TALB, TCON, TDES, TDRC, TDRL, TGID, TIT2, TPE1, TPE2, WFED
    from mutagen.id3 import ID3NoHeaderError

    try:
        id3 = ID3(str(path))
    except ID3NoHeaderError:
        id3 = ID3()
    id3.setall("TIT2", [TIT2(encoding=3, text=tags.title)])
    id3.setall("TALB", [TALB(encoding=3, text=tags.show)])
    id3.setall("TPE1", [TPE1(encoding=3, text=tags.author)])
    id3.setall("TPE2", [TPE2(encoding=3, text=tags.show)])
    id3.setall("TCON", [TCON(encoding=3, text=PODCAST_GENRE)])
    id3.setall("PCST", [PCST(value=1)])
    id3.setall("WFED", [WFED(encoding=3, url=tags.feed_url)])
    id3.setall("TGID", [TGID(encoding=3, text=tags.guid)])
    if tags.date:
        id3.setall("TDRC", [TDRC(encoding=3, text=tags.date)])
        id3.setall("TDRL", [TDRL(encoding=3, text=tags.date)])
    if tags.description:
        id3.setall("TDES", [TDES(encoding=3, text=tags.description)])
        id3.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=tags.description)])
    id3.save(str(path), v2_version=3)  # v2.3: what most hardware players read best
