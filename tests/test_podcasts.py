from simple_jukebox.core.podcast_feed import Feed, FeedEpisode, parse_date, parse_duration, parse_feed
from simple_jukebox.core.podcasts import PodcastStore

FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Retro Hour</title>
    <itunes:author>Dan Wood</itunes:author>
    <description>&lt;p&gt;Retro &amp;amp; computing&lt;/p&gt;</description>
    <itunes:image href="https://example.com/art.jpg"/>
    <link>https://example.com</link>
    <item>
      <title>Older episode</title>
      <guid>ep-1</guid>
      <pubDate>Mon, 05 Oct 2026 08:00:00 +0000</pubDate>
      <enclosure url="https://example.com/1.mp3" length="1234" type="audio/mpeg"/>
      <itunes:duration>1:02:03</itunes:duration>
    </item>
    <item>
      <title>Newer episode</title>
      <pubDate>Thu, 08 Oct 2026 08:00:00 GMT</pubDate>
      <enclosure url="https://example.com/2.m4a" type="audio/x-m4a"/>
      <itunes:duration>2823</itunes:duration>
      <description>&lt;b&gt;Show&lt;/b&gt; notes</description>
    </item>
    <item><title>No audio, not an episode</title></item>
  </channel>
</rss>"""


def test_parse_feed_reads_show_and_episodes_newest_first():
    feed = parse_feed(FEED_XML)
    assert feed.title == "Retro Hour"
    assert feed.author == "Dan Wood"
    assert feed.image_url == "https://example.com/art.jpg"
    assert feed.description == "Retro & computing"
    assert [e.title for e in feed.episodes] == ["Newer episode", "Older episode"]
    newer, older = feed.episodes
    assert newer.guid == "https://example.com/2.m4a"  # no <guid>: the audio URL stands in
    assert newer.duration == 2823 and newer.description == "Show notes"
    assert older.duration == 3723 and older.file_size == 1234 and older.mime_type == "audio/mpeg"


def test_parse_feed_rejects_non_feeds():
    import pytest

    from simple_jukebox.core.podcast_feed import FeedError

    with pytest.raises(FeedError):
        parse_feed(b"<html><body>not a feed</body></html>")
    with pytest.raises(FeedError):
        parse_feed(b"not xml at all")


def test_date_and_duration_parsing():
    assert parse_date("Thu, 08 Oct 2026 08:00:00 GMT") > parse_date("Mon, 05 Oct 2026 08:00:00 +0000")
    assert parse_date("yesterday-ish") == 0.0
    assert parse_duration("45:10") == 2710
    assert parse_duration("nonsense") == 0


def _feed(*guids):
    return Feed(
        title="Show",
        episodes=[
            FeedEpisode(guid=g, title=f"Episode {g}", audio_url=f"https://x/{g}.mp3", published=1000 + i)
            for i, g in enumerate(guids)
        ],
    )


def test_subscribe_downloads_only_the_newest_and_refresh_finds_new(tmp_path):
    store = PodcastStore(tmp_path / "p.sqlite3")
    podcast_id, to_download = store.subscribe("https://x/feed", _feed("a", "b", "c"))
    assert [store.episode(i).guid for i in to_download] == ["c"]
    assert [e.guid for e in store.episodes(podcast_id)] == ["c", "b", "a"]

    new = store.update_from_feed(podcast_id, _feed("a", "b", "c", "d"))
    assert [store.episode(i).guid for i in new] == ["d"]
    # Subscribing again to the same feed just refreshes it.
    again_id, again_new = store.subscribe("https://x/feed", _feed("a", "b", "c", "d"))
    assert again_id == podcast_id and again_new == []


def test_downloads_played_and_position(tmp_path):
    store = PodcastStore(tmp_path / "p.sqlite3")
    podcast_id, _ = store.subscribe("https://x/feed", _feed("a", "b"))
    newest, older = store.episodes(podcast_id)
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"x")
    store.set_downloaded(newest.id, str(audio))
    store.set_position(newest.id, 61_000)

    episode = store.episode(newest.id)
    assert episode.downloaded and episode.position_ms == 61_000 and episode.podcast_title == "Show"
    assert store.podcasts()[0].downloaded_count == 1 and store.podcasts()[0].unplayed_count == 1
    assert [e.id for e in store.downloaded_episodes(unplayed_only=True)] == [newest.id]

    store.set_played(newest.id, True)
    assert store.episode(newest.id).position_ms == 0
    assert store.downloaded_episodes(unplayed_only=True) == []
    assert store.clear_download(newest.id) == str(audio)
    assert not store.episode(newest.id).downloaded


def test_keep_newest_downloads(tmp_path):
    store = PodcastStore(tmp_path / "p.sqlite3")
    podcast_id, _ = store.subscribe("https://x/feed", _feed("a", "b", "c"))
    for episode in store.episodes(podcast_id):
        store.set_downloaded(episode.id, f"/tmp/{episode.guid}.mp3")
    assert store.downloads_beyond_limit(podcast_id) == []  # 0 = keep all
    store.set_podcast_options(podcast_id, auto_download=True, keep_downloads=2)
    assert [store.episode(i).guid for i in store.downloads_beyond_limit(podcast_id)] == ["a"]


def test_unsubscribe_returns_files_and_removes_episodes(tmp_path):
    store = PodcastStore(tmp_path / "p.sqlite3")
    podcast_id, _ = store.subscribe("https://x/feed", _feed("a"))
    store.set_downloaded(store.episodes(podcast_id)[0].id, "/tmp/a.mp3")
    assert store.unsubscribe(podcast_id) == ["/tmp/a.mp3"]
    assert store.podcasts() == [] and store.episodes(podcast_id) == []


# --- downloading and tagging ---------------------------------------------------

import struct
from pathlib import Path

from simple_jukebox.core.podcast_download import (
    episode_extension,
    episode_relative_path,
    mp4_atoms,
    podcast_tags,
    tag_as_podcast,
)
from simple_jukebox.core.podcasts import Episode


def _episode(**overrides):
    values = dict(
        id=1, podcast_id=1, guid="guid-1", title="Episode: One?", audio_url="https://cdn.example.com/ep1.mp3?x=1",
        published=1791446400.0, duration=60, file_size=0, mime_type="audio/mpeg",
        description="All about the C64", local_path=None, played=False, position_ms=0,
    )
    values.update(overrides)
    return Episode(**values)


def test_episode_paths_and_extensions():
    episode = _episode()
    assert episode_relative_path("Retro/Hour", episode) == "Retro_Hour/2026-10-08 Episode_ One_.mp3"
    assert episode_extension(_episode(audio_url="https://x/stream?id=5", mime_type="audio/x-m4a")) == ".m4a"
    assert episode_extension(_episode(audio_url="https://x/stream", mime_type="")) == ".mp3"


def test_mp3_is_tagged_as_a_podcast(tmp_path):
    from mutagen.id3 import ID3

    path = tmp_path / "ep.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 400)  # an MPEG frame header + padding
    tags = podcast_tags("Retro Hour", "Dan Wood", "https://example.com/feed", _episode())
    assert tag_as_podcast(path, tags)

    id3 = ID3(str(path))
    assert id3["PCST"].value == 1
    assert str(id3["TCON"]) == "Podcast"
    assert str(id3["TALB"]) == "Retro Hour"
    assert str(id3["TPE1"]) == "Dan Wood"
    assert id3["WFED"].url == "https://example.com/feed"
    assert str(id3["TGID"]) == "guid-1"
    assert str(id3["TDRC"]) == "2026-10-08"


def _minimal_flac(path: Path) -> None:
    # "fLaC" + a last-metadata-block STREAMINFO (44.1kHz, stereo, 16-bit).
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00\x00\x00" * 2
    sample_info = (44100 << 44) | (1 << 41) | (15 << 36)
    streaminfo += sample_info.to_bytes(8, "big") + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def test_flac_and_ogg_style_tags(tmp_path):
    import mutagen

    path = tmp_path / "ep.flac"
    _minimal_flac(path)
    assert tag_as_podcast(path, podcast_tags("Retro Hour", "", "https://f", _episode()))
    audio = mutagen.File(str(path))
    assert audio["genre"] == ["Podcast"]
    assert audio["album"] == ["Retro Hour"]
    assert audio["artist"] == ["Retro Hour"]  # no author: the show stands in


def test_m4a_atoms_mark_a_podcast():
    atoms = mp4_atoms(podcast_tags("Retro Hour", "Dan", "https://f", _episode()))
    assert atoms["pcst"] is True
    assert atoms["stik"] == [21]
    assert atoms["\xa9gen"] == ["Podcast"]
    assert atoms["\xa9alb"] == ["Retro Hour"]


def test_untaggable_files_are_reported_not_raised(tmp_path):
    path = tmp_path / "ep.wav"
    path.write_bytes(b"not really")
    assert tag_as_podcast(path, podcast_tags("S", "", "", _episode())) is False
    broken = tmp_path / "ep.m4a"
    broken.write_bytes(b"not an mp4")
    assert tag_as_podcast(broken, podcast_tags("S", "", "", _episode())) is False


def test_download_episode_and_cancel(tmp_path):
    import pytest

    from simple_jukebox.core.podcast_download import DownloadCancelled, download_episode

    source = tmp_path / "remote.mp3"
    source.write_bytes(b"a" * 600_000)
    episode = _episode(audio_url=source.as_uri())
    seen = []
    target = download_episode(episode, tmp_path / "out" / "ep.mp3", progress=lambda d, t: seen.append((d, t)))
    assert target.read_bytes() == source.read_bytes()
    assert seen[-1] == (600_000, 600_000)

    with pytest.raises(DownloadCancelled):
        download_episode(episode, tmp_path / "out" / "ep2.mp3", cancelled=lambda: True)
    assert not list((tmp_path / "out").glob("ep2*"))  # no partial file left

    with pytest.raises(OSError):
        download_episode(_episode(audio_url=(tmp_path / "missing.mp3").as_uri()), tmp_path / "out" / "ep3.mp3")
    assert not list((tmp_path / "out").glob("ep3*"))
