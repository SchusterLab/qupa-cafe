"""The tray icon on Linux, as a StatusNotifierItem over D-Bus.

The StatusNotifierItem protocol, with its menu over com.canonical.dbusmenu,
is what KDE Plasma, XFCE, Cinnamon, LXQt, Budgie and MATE show natively, and
GNOME through the AppIndicator extension, which Ubuntu enables by default.
pystray reaches it only through PyGObject and a system typelib, which a
virtual environment lacks and which need root to install; this speaks it
with jeepney, which is pure Python. ``icon_class()`` is a pystray icon, so
the tray builds its menu with pystray's ``Menu`` and ``MenuItem`` either way.
Importing pystray chooses its backend, which fails where none works, so it
is imported only once the tray has chosen pystray's dummy one.
"""

from __future__ import annotations

import os
import threading

from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, new_error, new_method_call, new_method_return, new_signal
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
PROPERTIES = "org.freedesktop.DBus.Properties"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
# The sizes offered; the host scales the nearest to its panel.
SIZES = (22, 32, 48, 64)

INTROSPECTION = f"""<node>
 <interface name="{ITEM}">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewTitle"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
 </interface>
 <interface name="{MENU}">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{{sv}})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 </interface>
</node>"""


def watcher_present() -> bool | None:
    """Whether the session has a StatusNotifierItem host to show the icon,
    or None where there is no session bus to ask."""
    try:
        with open_dbus_connection(bus="SESSION") as connection:
            reply = connection.send_and_get_reply(message_bus.NameHasOwner(WATCHER), timeout=3)
    except Exception:  # noqa: BLE001 - no bus, or one that does not answer
        return None
    return bool(reply.body[0])


def pixmaps(image) -> list[tuple[int, int, bytes]]:
    """The image at each of SIZES as (width, height, ARGB32 in network byte
    order), the form IconPixmap takes."""
    from PIL import Image

    found = []
    for size in SIZES:
        red, green, blue, alpha = image.convert("RGBA").resize((size, size), Image.LANCZOS).split()
        found.append((size, size, Image.merge("RGBA", (alpha, red, green, blue)).tobytes()))
    return found


def label(text: str) -> str:
    """A menu label as dbusmenu takes it: an underscore marks a mnemonic, so
    a literal one is doubled."""
    return text.replace("_", "__")


def icon_class() -> type:
    """The pystray icon class shown as a StatusNotifierItem."""
    from pystray._base import Icon as BaseIcon
    from pystray._base import Menu

    # Item first, so that its methods replace the base class's unimplemented ones.
    class Icon(Item, BaseIcon):
        pass

    Item.separator = Menu.SEPARATOR
    return Icon


class Item:
    """What makes a pystray icon a StatusNotifierItem, mixed into one by
    icon_class()."""

    separator = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connection = None
        self._sending = threading.Lock()
        self._stopping = threading.Event()
        self._revision = 1
        self._items: dict[int, object] = {}
        self._pixmaps: list = []
        self._service = f"{ITEM}-{os.getpid()}-1"
        self._status = "Active"

    # ------------------------------------------------------------------ D-Bus

    def _send(self, message) -> None:
        if self._connection is not None:
            with self._sending:
                self._connection.send(message)

    def _signal(self, path: str, interface: str, member: str, signature: str | None = None, body: tuple = ()) -> None:
        self._send(new_signal(DBusAddress(path, interface=interface), member, signature, body))

    def _register(self) -> None:
        watcher = DBusAddress("/StatusNotifierWatcher", bus_name=WATCHER, interface=WATCHER)
        self._send(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (self._service,)))

    # ------------------------------------------------------------ properties

    def _tooltip(self) -> tuple:
        return ("", [], "Qupa Cafe", self.title or "")

    def _item_properties(self) -> dict[str, tuple]:
        return {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", self.name),
            "Title": ("s", self.title or self.name),
            "Status": ("s", self._status),
            "WindowId": ("i", 0),
            "IconName": ("s", ""),
            "IconPixmap": ("a(iiay)", self._pixmaps),
            "ToolTip": ("(sa(iiay)ss)", self._tooltip()),
            "ItemIsMenu": ("b", True),
            "Menu": ("o", MENU_PATH),
        }

    @staticmethod
    def _menu_properties() -> dict[str, tuple]:
        return {
            "Version": ("u", 3),
            "TextDirection": ("s", "ltr"),
            "Status": ("s", "normal"),
            "IconThemePath": ("as", []),
        }

    # ------------------------------------------------------------------ menu

    def _layout(self) -> tuple:
        """The menu as dbusmenu's (id, properties, children), numbering its
        items afresh."""
        self._items = {}
        children = []
        for number, item in enumerate(self.menu.items if self.menu else (), start=1):
            if not item.visible:
                continue
            self._items[number] = item
            if item is self.separator:
                properties = {"type": ("s", "separator")}
            else:
                properties = {"label": ("s", label(str(item.text))), "enabled": ("b", bool(item.enabled))}
            children.append(("(ia{sv}av)", (number, properties, [])))
        return (0, {"children-display": ("s", "submenu")}, children)

    def _clicked(self, number: int) -> None:
        item = self._items.get(number)
        if item is not None and item is not self.separator and item.enabled:
            item(self)

    # -------------------------------------------------------------- dispatch

    def _answer(self, message):
        """The reply to a method call, or None for one that needs none."""
        header = message.header
        path = header.fields.get(HeaderFields.path)
        interface = header.fields.get(HeaderFields.interface)
        member = header.fields.get(HeaderFields.member)
        body = message.body

        if interface == INTROSPECTABLE and member == "Introspect":
            return new_method_return(message, "s", (INTROSPECTION,))

        if interface == PROPERTIES:
            values = self._item_properties() if path == ITEM_PATH else self._menu_properties()
            if member == "GetAll":
                return new_method_return(message, "a{sv}", (values,))
            if member == "Get" and body[1] in values:
                return new_method_return(message, "v", (values[body[1]],))
            return new_error(message, "org.freedesktop.DBus.Error.UnknownProperty", "s", (str(body[1:2]),))

        if path == MENU_PATH and interface in (MENU, None):
            if member == "GetLayout":
                return new_method_return(message, "u(ia{sv}av)", (self._revision, self._layout()))
            if member == "GetGroupProperties":
                found = [child[1][:2] for child in self._layout()[2] if child[1][0] in body[0]]
                return new_method_return(message, "a(ia{sv})", (found,))
            if member == "GetProperty":
                for child in self._layout()[2]:
                    if child[1][0] == body[0] and body[1] in child[1][1]:
                        return new_method_return(message, "v", (child[1][1][body[1]],))
                return new_error(message, "org.freedesktop.DBus.Error.InvalidArgs", "s", ("no such property",))
            if member == "Event":
                if body[1] == "clicked":
                    self._clicked(body[0])
                return new_method_return(message)
            if member == "EventGroup":
                for number, event, _data, _time in body[0]:
                    if event == "clicked":
                        self._clicked(number)
                return new_method_return(message, "ai", ([],))
            if member == "AboutToShow":
                return new_method_return(message, "b", (False,))
            if member == "AboutToShowGroup":
                return new_method_return(message, "aiai", ([], []))

        if path == ITEM_PATH and interface in (ITEM, None) and member in ("Activate", "SecondaryActivate", "ContextMenu", "Scroll"):
            return new_method_return(message)

        return new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s", (f"{interface}.{member}",))

    # ------------------------------------------------------- pystray's hooks

    def _run(self):
        self._pixmaps = pixmaps(self.icon) if self.icon else []
        self._connection = open_dbus_connection(bus="SESSION")
        self._connection.send_and_get_reply(message_bus.RequestName(self._service))
        # A host that starts, or starts again, after the icon is told of it.
        rule = MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus",
                         member="NameOwnerChanged", path="/org/freedesktop/DBus")
        rule.add_arg_condition(0, WATCHER)
        self._connection.send_and_get_reply(message_bus.AddMatch(rule))
        self._register()
        self._mark_ready()
        try:
            while not self._stopping.is_set():
                try:
                    message = self._connection.receive(timeout=0.5)
                except TimeoutError:
                    continue
                kind = message.header.message_type
                if kind == MessageType.method_call:
                    reply = self._answer(message)
                    if reply is not None:
                        self._send(reply)
                elif kind == MessageType.signal and message.header.fields.get(HeaderFields.member) == "NameOwnerChanged":
                    if message.body[0] == WATCHER and message.body[2]:
                        self._register()
        finally:
            connection, self._connection = self._connection, None
            connection.close()

    def _stop(self):
        self._stopping.set()

    def _show(self):
        self._status = "Active"
        self._signal(ITEM_PATH, ITEM, "NewStatus", "s", (self._status,))

    def _hide(self):
        self._status = "Passive"
        self._signal(ITEM_PATH, ITEM, "NewStatus", "s", (self._status,))

    def _update_icon(self):
        self._pixmaps = pixmaps(self.icon) if self.icon else []
        self._icon_valid = True
        self._signal(ITEM_PATH, ITEM, "NewIcon")

    def _update_title(self):
        self._signal(ITEM_PATH, ITEM, "NewTitle")
        self._signal(ITEM_PATH, ITEM, "NewToolTip")

    def _update_menu(self):
        self._revision += 1
        self._signal(MENU_PATH, MENU, "LayoutUpdated", "ui", (self._revision, 0))

    def _notify(self, message, title=None):
        notifications = DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                                    interface="org.freedesktop.Notifications")
        self._send(new_method_call(notifications, "Notify", "susssasa{sv}i",
                                   ("Qupa Cafe", 0, "", title or "Qupa Cafe", message, [], {}, -1)))

    def _remove_notification(self):
        pass
