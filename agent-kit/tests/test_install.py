"""The installer: what it registers, and in which form."""

from __future__ import annotations

import qupa_cafe.install as install


def test_a_console_script_is_found_in_a_conda_layout(monkeypatch, tmp_path):
    """``python.exe`` sits at the environment root there, not beside the scripts.

    Missing it means falling back to ``python -m``, which is not the same
    thing: ``-m`` puts the working directory on ``sys.path``, so an agent
    started inside a checkout of this repository would load that copy of the
    package rather than the installed one. A console script has no such
    ambiguity, which is the reason to prefer it.
    """
    env = tmp_path / "envs" / "default"
    (env / "Scripts").mkdir(parents=True)
    (env / "python.exe").write_text("")
    script = env / "Scripts" / "qupa-daemon.exe"
    script.write_text("")

    monkeypatch.setattr(install.sys, "executable", str(env / "python.exe"))

    assert install.console_script("qupa-daemon") == script


def test_a_console_script_is_found_beside_the_interpreter(monkeypatch, tmp_path):
    """Which is where a virtualenv puts it, on either platform."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("")
    script = bindir / "qupa-mcp"
    script.write_text("")

    monkeypatch.setattr(install.sys, "executable", str(bindir / "python"))

    assert install.console_script("qupa-mcp") == script


def test_a_missing_console_script_is_reported_as_absent(monkeypatch, tmp_path):
    """So the caller falls back rather than registering a path to nothing."""
    bindir = tmp_path / "empty"
    bindir.mkdir()
    monkeypatch.setattr(install.sys, "executable", str(bindir / "python"))

    assert install.console_script("qupa-daemon") is None
