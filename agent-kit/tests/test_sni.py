"""The tray icon as a StatusNotifierItem: its menu, properties and picture,
answered as D-Bus would ask, with no bus."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("jeepney")
os.environ.setdefault("PYSTRAY_BACKEND", "dummy")
pystray = pytest.importorskip("pystray")
PIL = pytest.importorskip("PIL")

from jeepney import DBusAddress, MessageType, new_method_call  # noqa: E402

from qupa_cafe import sni  # noqa: E402


@pytest.fixture
def icon():
    from PIL import Image

    clicked = []
    menu = pystray.Menu(
        pystray.MenuItem("Show wakes", lambda icon, item: clicked.append("wakes")),
        pystray.MenuItem("Greyed", lambda icon, item: clicked.append("greyed"), enabled=False),
        pystray.MenuItem("Hidden", lambda icon, item: None, visible=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("snake_case", lambda icon, item: clicked.append("snake")),
    )
    made = sni.icon_class()("qupa-cafe", Image.new("RGBA", (64, 64), (255, 0, 0, 255)), "Qupa Cafe: polling", menu)
    made.clicked = clicked
    return made


def call(icon, path, interface, member, signature=None, body=()):
    message = new_method_call(DBusAddress(path, bus_name=":1.1", interface=interface), member, signature, body)
    return icon._answer(message)


def test_the_menu_is_laid_out_as_dbusmenu_asks(icon):
    reply = call(icon, sni.MENU_PATH, sni.MENU, "GetLayout", "iias", (0, -1, []))
    revision, (root, properties, children) = reply.body
    labels = [child[1][1].get("label", child[1][1].get("type")) for child in children]
    # Hidden items are left out, and an underscore is doubled, not a mnemonic.
    assert labels == [("s", "Show wakes"), ("s", "Greyed"), ("s", "separator"), ("s", "snake__case")]
    assert children[1][1][1]["enabled"] == ("b", False)


def test_a_click_runs_the_item_and_not_a_greyed_one(icon):
    call(icon, sni.MENU_PATH, sni.MENU, "GetLayout", "iias", (0, -1, []))
    call(icon, sni.MENU_PATH, sni.MENU, "Event", "isvu", (1, "clicked", ("i", 0), 0))
    call(icon, sni.MENU_PATH, sni.MENU, "Event", "isvu", (2, "clicked", ("i", 0), 0))
    assert icon.clicked == ["wakes"]


def test_the_item_s_properties_carry_the_menu_and_the_tooltip(icon):
    reply = call(icon, sni.ITEM_PATH, "org.freedesktop.DBus.Properties", "GetAll", "s", (sni.ITEM,))
    values = reply.body[0]
    assert values["Menu"] == ("o", sni.MENU_PATH) and values["ItemIsMenu"] == ("b", True)
    assert values["ToolTip"][1][3] == "Qupa Cafe: polling"


def test_an_unknown_method_is_an_error_not_silence(icon):
    assert call(icon, sni.ITEM_PATH, sni.ITEM, "Nonsense").header.message_type == MessageType.error


def test_the_picture_is_argb_at_each_size(icon):
    found = sni.pixmaps(icon.icon)
    assert [size[:2] for size in found] == [(n, n) for n in sni.SIZES]
    width, height, data = found[0]
    # Opaque red: alpha, then red, green, blue.
    assert len(data) == width * height * 4 and data[:4] == bytes([255, 255, 0, 0])
