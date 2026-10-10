"""MPRIS: letting the Linux desktop control playback.

MPRIS (org.mpris.MediaPlayer2 on the D-Bus session bus) is how keyboard
media keys, the desktop's media controls (COSMIC/GNOME/KDE panels, the
lock screen) and tools like playerctl find and drive a music player —
whether or not its window has focus.

MprisState holds the properties and computes what changed (plain Python,
testable anywhere). MprisService serves them over D-Bus with jeepney on a
background thread and hands incoming commands to a callback; the caller
(the GUI) must marshal those onto its own thread. Linux only.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Optional

BUS_NAME = "org.mpris.MediaPlayer2.simple_jukebox"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECT_IFACE = "org.freedesktop.DBus.Introspectable"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

LOOP_FOR_REPEAT = {"off": "None", "all": "Playlist", "one": "Track"}
REPEAT_FOR_LOOP = {v: k for k, v in LOOP_FOR_REPEAT.items()}

# Variants are jeepney-style (signature, value) pairs.
Variant = tuple


def track_object_path(kind: str, item_id: int) -> str:
    return f"/org/simple_jukebox/{kind}/{int(item_id)}"


def build_metadata(
    track_path: Optional[str],
    title: str = "",
    artists: Optional[list[str]] = None,
    album: str = "",
    album_artists: Optional[list[str]] = None,
    length_us: int = 0,
    art_url: str = "",
    track_number: Optional[int] = None,
    url: str = "",
) -> dict[str, Variant]:
    """MPRIS Metadata (a{sv}). track_path None = nothing loaded."""
    if track_path is None:
        return {"mpris:trackid": ("o", NO_TRACK)}
    metadata: dict[str, Variant] = {
        "mpris:trackid": ("o", track_path),
        "xesam:title": ("s", title),
    }
    if artists:
        metadata["xesam:artist"] = ("as", list(artists))
    if album:
        metadata["xesam:album"] = ("s", album)
    if album_artists:
        metadata["xesam:albumArtist"] = ("as", list(album_artists))
    if length_us > 0:
        metadata["mpris:length"] = ("x", int(length_us))
    if art_url:
        metadata["mpris:artUrl"] = ("s", art_url)
    if track_number:
        metadata["xesam:trackNumber"] = ("i", int(track_number))
    if url:
        metadata["xesam:url"] = ("s", url)
    return metadata


class MprisState:
    """The two interfaces' properties, kept current by the player."""

    def __init__(self, identity: str = "Simple-Jukebox", desktop_entry: str = "simple-jukebox"):
        self.root: dict[str, Variant] = {
            "CanQuit": ("b", True),
            "CanRaise": ("b", True),
            "CanSetFullscreen": ("b", False),
            "Fullscreen": ("b", False),
            "HasTrackList": ("b", False),
            "Identity": ("s", identity),
            "DesktopEntry": ("s", desktop_entry),
            "SupportedUriSchemes": ("as", []),
            "SupportedMimeTypes": ("as", []),
        }
        self.player: dict[str, Variant] = {
            "PlaybackStatus": ("s", "Stopped"),
            "LoopStatus": ("s", "None"),
            "Rate": ("d", 1.0),
            "MinimumRate": ("d", 1.0),
            "MaximumRate": ("d", 1.0),
            "Shuffle": ("b", False),
            "Metadata": ("a{sv}", build_metadata(None)),
            "Volume": ("d", 1.0),
            "CanGoNext": ("b", False),
            "CanGoPrevious": ("b", False),
            "CanPlay": ("b", True),
            "CanPause": ("b", False),
            "CanSeek": ("b", False),
            "CanControl": ("b", True),
        }
        self.position_us = 0  # read live; per the spec it's never announced as "changed"
        self._lock = threading.Lock()

    def interface(self, name: str) -> Optional[dict[str, Variant]]:
        return {ROOT_IFACE: self.root, PLAYER_IFACE: self.player}.get(name)

    def get(self, interface: str, prop: str) -> Optional[Variant]:
        with self._lock:
            if interface == PLAYER_IFACE and prop == "Position":
                return ("x", int(self.position_us))
            props = self.interface(interface)
            return None if props is None else props.get(prop)

    def get_all(self, interface: str) -> dict[str, Variant]:
        with self._lock:
            props = self.interface(interface)
            if props is None:
                return {}
            result = dict(props)
            if interface == PLAYER_IFACE:
                result["Position"] = ("x", int(self.position_us))
            return result

    def update(self, **changes) -> dict[str, Variant]:
        """Set player properties by name (plain values; Metadata as built
        by build_metadata); returns those whose value actually changed,
        to announce."""
        changed: dict[str, Variant] = {}
        with self._lock:
            for name, value in changes.items():
                old = self.player.get(name)
                if old is None:
                    continue
                new = (old[0], value)
                if new != old:
                    self.player[name] = new
                    changed[name] = new
        return changed

    def set_position(self, position_us: int) -> None:
        with self._lock:
            self.position_us = max(0, int(position_us))


INTROSPECTION_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg name="data" direction="out" type="s"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="out" type="v"/></method>
    <method name="GetAll"><arg direction="in" type="s"/><arg direction="out" type="a{sv}"/></method>
    <method name="Set"><arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="in" type="v"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/>
    <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg name="Offset" direction="in" type="x"/></method>
    <method name="SetPosition"><arg name="TrackId" direction="in" type="o"/><arg name="Position" direction="in" type="x"/></method>
    <method name="OpenUri"><arg name="Uri" direction="in" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

# Player methods → the command name handed to the callback.
_COMMANDS = {
    (ROOT_IFACE, "Raise"): "raise",
    (ROOT_IFACE, "Quit"): "quit",
    (PLAYER_IFACE, "Next"): "next",
    (PLAYER_IFACE, "Previous"): "previous",
    (PLAYER_IFACE, "Pause"): "pause",
    (PLAYER_IFACE, "PlayPause"): "play_pause",
    (PLAYER_IFACE, "Stop"): "stop",
    (PLAYER_IFACE, "Play"): "play",
}
# Writable properties → command name; the new value is passed along.
_SETTERS = {"Shuffle": "set_shuffle", "LoopStatus": "set_loop", "Volume": "set_volume"}


class MprisService:
    """Serves MprisState on the session bus. on_command(name, argument)
    is called on the D-Bus thread for each request from the desktop."""

    def __init__(self, state: MprisState, on_command: Callable[[str, object], None]):
        self.state = state
        self._on_command = on_command
        self._connection = None
        # One connection, read by the D-Bus thread while the GUI thread
        # also sends signals on it: sends are serialised so two messages
        # never interleave on the socket.
        self._send_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    def start(self) -> bool:
        """Connect and claim the bus name. False (quietly) if there's no
        session bus or jeepney — media keys just won't reach the app."""
        try:
            from jeepney.bus_messages import message_bus
            from jeepney.io.blocking import open_dbus_connection

            self._connection = open_dbus_connection(bus="SESSION")
            name = BUS_NAME
            # 4 = DBUS_NAME_FLAG_DO_NOT_QUEUE. A second running copy gets
            # its own name, as MPRIS asks for multiple instances.
            reply = self._connection.send_and_get_reply(message_bus.RequestName(name, 4), timeout=5)
            if reply.body[0] not in (1, 4):  # primary owner / already owner
                name = f"{BUS_NAME}.instance{os.getpid()}"
                self._connection.send_and_get_reply(message_bus.RequestName(name, 4), timeout=5)
        except Exception:
            self._connection = None
            return False
        self._thread = threading.Thread(target=self._serve, name="mpris", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stopped = True
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass

    # --- outgoing ---------------------------------------------------------

    def announce(self, changed: dict[str, Variant]) -> None:
        """Emit PropertiesChanged for player properties that changed."""
        if changed:
            self._signal(PROPS_IFACE, "PropertiesChanged", "sa{sv}as", (PLAYER_IFACE, changed, []))

    def seeked(self, position_us: int) -> None:
        self.state.set_position(position_us)
        self._signal(PLAYER_IFACE, "Seeked", "x", (int(position_us),))

    def _signal(self, interface: str, member: str, signature: str, body: tuple) -> None:
        if self._connection is None or self._stopped:
            return
        from jeepney import DBusAddress, new_signal

        try:
            self._send(new_signal(DBusAddress(OBJECT_PATH, interface=interface), member, signature, body))
        except Exception:
            pass  # the bus went away; nothing to tell

    # --- incoming ---------------------------------------------------------

    def _serve(self) -> None:
        from jeepney import HeaderFields, MessageType

        while not self._stopped:
            try:
                message = self._connection.receive()
            except Exception:
                return  # connection closed
            if message.header.message_type != MessageType.method_call:
                continue
            fields = message.header.fields
            try:
                self._handle(
                    message,
                    fields.get(HeaderFields.path),
                    fields.get(HeaderFields.interface),
                    fields.get(HeaderFields.member),
                )
            except Exception as error:  # never let one bad request stop the service
                self._reply_error(message, "org.freedesktop.DBus.Error.Failed", str(error))

    def _send(self, message) -> None:
        with self._send_lock:
            self._connection.send(message)

    def _handle(self, message, path, interface, member) -> None:
        from jeepney import new_method_return

        send = self._send
        if path != OBJECT_PATH:
            self._reply_error(message, "org.freedesktop.DBus.Error.UnknownObject", path or "")
            return
        if member == "Introspect":
            send(new_method_return(message, "s", (INTROSPECTION_XML,)))
        elif interface == PROPS_IFACE and member == "Get":
            iface, prop = message.body
            value = self.state.get(iface, prop)
            if value is None:
                self._reply_error(message, "org.freedesktop.DBus.Error.UnknownProperty", prop)
            else:
                send(new_method_return(message, "v", (value,)))
        elif interface == PROPS_IFACE and member == "GetAll":
            send(new_method_return(message, "a{sv}", (self.state.get_all(message.body[0]),)))
        elif interface == PROPS_IFACE and member == "Set":
            iface, prop, (_signature, value) = message.body
            if iface == PLAYER_IFACE and prop in _SETTERS:
                self._on_command(_SETTERS[prop], value)
            send(new_method_return(message))
        elif (interface, member) in _COMMANDS or (interface is None and (PLAYER_IFACE, member) in _COMMANDS):
            command = _COMMANDS.get((interface, member)) or _COMMANDS[(PLAYER_IFACE, member)]
            send(new_method_return(message))
            self._on_command(command, None)
        elif member == "Seek":
            send(new_method_return(message))
            self._on_command("seek", int(message.body[0]))
        elif member == "SetPosition":
            track_id, position = message.body
            send(new_method_return(message))
            self._on_command("set_position", (track_id, int(position)))
        elif member == "OpenUri":
            send(new_method_return(message))
        else:
            self._reply_error(message, "org.freedesktop.DBus.Error.UnknownMethod", member or "")

    def _reply_error(self, message, name: str, text: str) -> None:
        from jeepney import new_error

        try:
            self._send(new_error(message, name, "s", (text,)))
        except Exception:
            pass
