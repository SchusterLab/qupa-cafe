"""Updating the kit in place, as the tray's menu does."""

from __future__ import annotations

from pathlib import Path

import pytest

from qupa_cafe import __version__, update


@pytest.fixture
def ran(monkeypatch):
    """Every command update() would run, with pip answering as a test says."""
    calls: list[list[str]] = []
    outcome = {"pip": 0, "after": __version__}

    def fake_run(argv):
        calls.append(argv)
        return outcome["pip"] if "pip" in argv else 0

    monkeypatch.setattr(update, "run", fake_run)
    monkeypatch.setattr(update, "installed_version", lambda: outcome["after"])
    monkeypatch.setattr(update, "installed_from_index", lambda: True)
    monkeypatch.setattr(update, "tray_available", lambda: True)
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
    assert calls[0][1:] == ["-m", "pip", "install", "--upgrade", "qupa-cafe[tray]"]


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
