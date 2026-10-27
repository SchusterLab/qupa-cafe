"""qupa-uninstall: what it stops, deletes and leaves."""

from __future__ import annotations

import pytest

from qupa_cafe import install, sandbox, uninstall


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """The kit's files in a temporary home, with nothing to stop."""
    config, data, mcp = tmp_path / "config", tmp_path / "data", tmp_path / "qupa-cafe.mcp.json"
    (data / "sandbox" / "workspace").mkdir(parents=True)
    (data / "daemon.sqlite3").write_text("", encoding="ascii")
    config.mkdir()
    (config / "env").write_text("QUPA_AGENT_DIR=/notes\n", encoding="utf-8")
    mcp.write_text("{}", encoding="ascii")
    skill = tmp_path / "skills" / "qupa-cafe" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---", encoding="ascii")
    monkeypatch.setattr(uninstall, "SKILL_PATH", skill)
    monkeypatch.setattr(uninstall, "CONFIG_DIR", config)
    monkeypatch.setattr(install, "ENV_PATH", config / "env")
    monkeypatch.setattr(uninstall, "DATA_DIR", data)
    monkeypatch.setattr(uninstall, "CLAUDE_MCP_PATH", mcp)
    stopped = []
    monkeypatch.setattr(uninstall, "unregister_tray", lambda: stopped.append("tray"))
    monkeypatch.setattr(uninstall, "unregister_daemon", lambda: stopped.append("daemon"))
    monkeypatch.setattr(uninstall, "unregister_servers", lambda: stopped.append("servers"))
    monkeypatch.setattr(sandbox, "bwrap_exempted", lambda: False)
    return {"config": config, "data": data, "mcp": mcp, "skill": skill.parent, "stopped": stopped}


def test_it_stops_everything_and_deletes_the_kit_s_files(installed, capsys):
    assert uninstall.uninstall(yes=True) == 0
    assert installed["stopped"] == ["tray", "daemon", "servers"]
    assert not any(installed[k].exists() for k in ("config", "data", "mcp", "skill"))
    out = capsys.readouterr().out
    assert "/notes" in out and "pip uninstall qupa-cafe" in out


def test_it_asks_first_and_a_no_removes_nothing(installed, monkeypatch):
    monkeypatch.setattr(install, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert uninstall.uninstall(yes=False) == 1
    assert installed["stopped"] == [] and installed["config"].exists() and installed["data"].exists()


def test_with_nobody_to_ask_it_needs_yes(installed, monkeypatch):
    monkeypatch.setattr(install, "interactive", lambda: False)
    assert uninstall.uninstall(yes=False) == 2
    assert installed["stopped"] == [] and installed["data"].exists()


def test_the_bubblewrap_exemption_is_named_with_the_commands_that_undo_it(installed, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "bwrap_exempted", lambda: True)
    uninstall.uninstall(yes=True)
    out = capsys.readouterr().out
    # The profile is unloaded while its file is still there to name it.
    assert out.index("apparmor_parser -R") < out.index(f"sudo rm -r {sandbox.LINUX_BWRAP.parent}")
