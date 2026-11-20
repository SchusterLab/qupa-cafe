"""The tray icon: its clicks, its log item, and the restart it asks for."""

from qupa_cafe import install, tray


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


def test_the_log_is_opened_when_there_is_one(monkeypatch, tmp_path):
    log = tmp_path / "daemon.log"
    monkeypatch.setenv("QUPA_LOG", str(log))
    opened = []
    monkeypatch.setattr(tray, "open_file", opened.append)
    settings = tray.Settings(state_path=str(tmp_path / "daemon.sqlite3"))

    assert "No daemon log yet" in tray.show_log(settings)
    log.write_text("polling\n", encoding="utf-8")
    assert tray.show_log(settings) is None
    assert opened == [log]


def test_a_restart_on_windows_stops_the_daemon_and_starts_what_the_run_key_starts(monkeypatch):
    done = []
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install, "run_entry", lambda name: '"pythonw.exe" ...')
    monkeypatch.setattr(install, "stop_running_daemon", lambda: done.append("stop") or True)
    monkeypatch.setattr(install, "start_detached", lambda argv: done.append(argv) or True)

    assert install.restart_daemon() == (True, "Restarted the daemon.")
    assert done[0] == "stop" and done[1][-1] == "qupa_cafe.daemon"


def test_a_restart_that_cannot_start_it_says_so(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install, "run_entry", lambda name: None)
    monkeypatch.setattr(install, "stop_running_daemon", lambda: True)
    monkeypatch.setattr(install, "start_daemon_now", lambda: False)

    ok, said = install.restart_daemon()
    assert not ok and "qupa-install" in said


def test_the_browsers_are_opened_120_columns_wide():
    assert tray.BROWSER_COLUMNS == 120
