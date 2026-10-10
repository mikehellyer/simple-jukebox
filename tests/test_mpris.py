import os
import threading

import pytest

from simple_jukebox.core.mpris import (
    NO_TRACK,
    PLAYER_IFACE,
    ROOT_IFACE,
    MprisService,
    MprisState,
    build_metadata,
    track_object_path,
)


def test_metadata_types_follow_the_spec():
    metadata = build_metadata(
        track_object_path("track", 7), title="Dream Police", artists=["Cheap Trick"], album="Dream Police",
        length_us=234_000_000, art_url="file:///tmp/a.jpg", track_number=1,
    )
    assert metadata["mpris:trackid"] == ("o", "/org/simple_jukebox/track/7")
    assert metadata["xesam:artist"] == ("as", ["Cheap Trick"])
    assert metadata["mpris:length"] == ("x", 234_000_000)
    assert metadata["xesam:trackNumber"] == ("i", 1)
    assert build_metadata(None) == {"mpris:trackid": ("o", NO_TRACK)}


def test_update_reports_only_real_changes():
    state = MprisState()
    assert state.update(PlaybackStatus="Playing", CanPause=True) == {
        "PlaybackStatus": ("s", "Playing"), "CanPause": ("b", True),
    }
    assert state.update(PlaybackStatus="Playing") == {}
    assert state.update(NotAProperty=1) == {}
    changed = state.update(Metadata=build_metadata("/org/simple_jukebox/track/1", title="A"))
    assert changed["Metadata"][0] == "a{sv}"


def test_position_is_live_but_never_announced():
    state = MprisState()
    state.set_position(5_000_000)
    assert state.get(PLAYER_IFACE, "Position") == ("x", 5_000_000)
    assert state.get_all(PLAYER_IFACE)["Position"] == ("x", 5_000_000)
    assert state.get(ROOT_IFACE, "Identity") == ("s", "Simple-Jukebox")
    assert state.get("org.example.Nope", "X") is None


def _session_bus_available():
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


@pytest.mark.skipif(not _session_bus_available(), reason="needs a D-Bus session bus (Linux desktop)")
def test_service_answers_desktop_requests_over_dbus():
    from jeepney import DBusAddress, Properties, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    commands = []
    received = threading.Event()

    def on_command(name, argument):
        commands.append((name, argument))
        received.set()

    state = MprisState()
    state.update(PlaybackStatus="Playing", Metadata=build_metadata("/org/simple_jukebox/track/3", title="Song"))
    service = MprisService(state, on_command)
    assert service.start()
    try:
        client = open_dbus_connection(bus="SESSION")
        # Find whichever name we got (a running Simple-Jukebox may hold the main one).
        from jeepney.bus_messages import message_bus

        names = client.send_and_get_reply(message_bus.ListNames()).body[0]
        ours = sorted(n for n in names if n.startswith("org.mpris.MediaPlayer2.simple_jukebox"))
        name = ours[-1] if any(".instance" in n for n in ours) else ours[0]
        if any(".instance" in n for n in ours):
            name = next(n for n in ours if n.endswith(f"instance{os.getpid()}"))
        player = DBusAddress("/org/mpris/MediaPlayer2", bus_name=name, interface=PLAYER_IFACE)

        reply = client.send_and_get_reply(Properties(player).get_all())
        props = reply.body[0]
        assert props["PlaybackStatus"] == ("s", "Playing")
        assert props["Metadata"][1]["xesam:title"] == ("s", "Song")

        client.send_and_get_reply(new_method_call(player, "PlayPause"))
        assert received.wait(2)
        received.clear()
        client.send_and_get_reply(new_method_call(player, "Seek", "x", (10_000_000,)))
        assert received.wait(2)
        client.send_and_get_reply(Properties(player).set("Shuffle", "b", True))
        assert commands == [("play_pause", None), ("seek", 10_000_000), ("set_shuffle", True)]
        client.close()
    finally:
        service.stop()
