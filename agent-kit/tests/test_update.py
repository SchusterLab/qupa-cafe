"""Updating the kit in place, as the tray's menu does."""

from __future__ import annotations

from pathlib import Path

import pytest

from qupa_cafe import __version__, update


@pytest.fixture
def ran(monkeypatch):
    """Every command update() would run, with pip answering as a test says."""
    calls: list[list[str]] = []
    outcome = {"pip": 0, "after": __version__, "latest": None}

    def fake_run(argv):
        calls.append(argv)
        return outcome["pip"] if "pip" in argv else 0

    monkeypatch.setattr(update, "run", fake_run)
    monkeypatch.setattr(update, "installed_version", lambda: outcome["after"])
    monkeypatch.setattr(update, "installed_from_index", lambda: True)
    monkeypatch.setattr(update, "tray_available", lambda: True)
    monkeypatch.setattr(update, "update_available", lambda timeout=3.0: outcome["latest"])
    return calls, outcome


def test_only_a_copy_pip_installed_counts_as_installed():
    assert update.installed_from_index(Path("/venv/lib/python3.12/site-packages/qupa_cafe"))
    assert update.installed_from_index(Path("/usr/lib/python3/dist-packages/qupa_cafe"))
    assert not update.installed_from_index(Path("/home/me/git/qupa-cafe/agent-kit/qupa_cafe"))


def test_a_checkout_is_refused_and_nothing_runs(ran, monkeypatch, capsys):
    """pip would install a copy the checkout then shadows, changing nothing."""
    calls, _ = ran
    monkeypatch.setattr(update, "installed_from_index", lambda: False)
    assert update.update() == 2
    assert calls == []
    assert "not from an installed package" in capsys.readouterr().out


def test_pip_upgrades_the_kit_with_the_tray_extra(ran):
    calls, outcome = ran
    outcome["after"] = "99.0.0"
    update.update()
    assert calls[0][1:] == ["-m", "pip", "install", "--upgrade", "--no-cache-dir", "qupa-cafe[tray]"]


def test_the_published_version_is_asked_for_by_number(ran):
    """pip's own copy of the index can lag the JSON the wake browser read,
    and then an unpinned upgrade finds nothing newer."""
    calls, outcome = ran
    outcome["latest"] = outcome["after"] = "99.0.0"
    update.update()
    assert calls[0][-1] == "qupa-cafe[tray]==99.0.0"


def test_a_version_pip_cannot_find_yet_is_said_to_be_on_its_way(ran, capsys):
    calls, outcome = ran
    outcome["latest"], outcome["pip"] = "99.0.0", 1
    assert update.update() == 1
    assert len(calls) == 1
    assert "try again shortly" in capsys.readouterr().out


def test_a_failed_pip_runs_nothing_else(ran, capsys):
    calls, outcome = ran
    outcome["pip"] = 1
    assert update.update() == 1
    assert len(calls) == 1
    assert "ensurepip" in capsys.readouterr().out


def test_already_the_newest_runs_no_installer(ran, capsys):
    calls, _ = ran
    assert update.update() == 0
    assert len(calls) == 1
    assert "already the newest" in capsys.readouterr().out


def test_an_upgrade_runs_the_installer_with_this_machines_settings(ran):
    """--non-interactive, which reads what load_env_file filled in."""
    calls, outcome = ran
    outcome["after"] = "99.0.0"
    assert update.update() == 0
    assert calls[1][-3:] == ["-m", "qupa_cafe.install", "--non-interactive"]


def test_the_version_is_read_from_a_fresh_interpreter(monkeypatch):
    """Which is how the upgrade is seen: this process still has the old code."""
    monkeypatch.setenv("PYTHONPATH", str(Path(update.__file__).resolve().parent.parent))
    assert update.installed_version() == __version__


def test_the_window_does_not_wait_without_a_terminal(monkeypatch):
    class NotATerminal:
        def isatty(self):
            return False

    monkeypatch.setattr(update.sys, "stdin", NotATerminal())
    update.pause()


def test_a_release_is_newer_than_its_own_development_builds():
    key = update.version_key
    assert key("0.11.0") > key("0.11.0.dev3")
    assert key("0.12.0") > key("0.11.0")
    assert key("0.10.0") > key("0.9.9")
    assert key("0.11") == key("0.11.0")


def test_an_update_is_offered_only_for_a_newer_published_kit(monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.11.0")
    monkeypatch.setattr(update, "latest_published", lambda timeout=3.0: "0.12.0")
    assert update.update_available() == "0.12.0"
    monkeypatch.setattr(update, "latest_published", lambda timeout=3.0: "0.11.0")
    assert update.update_available() is None
    monkeypatch.setattr(update, "latest_published", lambda timeout=3.0: None)
    assert update.update_available() is None
