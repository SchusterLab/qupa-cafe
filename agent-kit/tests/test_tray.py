from qupa_cafe import tray


class FakeWindowsIcon:
    def __init__(self):
        self.seen = []

    def _on_notify(self, wparam, lparam):
        self.seen.append(lparam)


def test_a_left_click_opens_the_menu_on_windows(monkeypatch):
    monkeypatch.setattr(tray.platform, "system", lambda: "Windows")
    icon = tray.menu_on_click(FakeWindowsIcon)()
    icon._on_notify(0, tray.WM_LBUTTONUP)
    icon._on_notify(0, tray.WM_RBUTTONUP)
    icon._on_notify(0, 0x0200)  # a mouse move passes through
    assert icon.seen == [tray.WM_RBUTTONUP, tray.WM_RBUTTONUP, 0x0200]


def test_elsewhere_the_icon_class_is_unchanged(monkeypatch):
    monkeypatch.setattr(tray.platform, "system", lambda: "Darwin")
    assert tray.menu_on_click(FakeWindowsIcon) is FakeWindowsIcon
