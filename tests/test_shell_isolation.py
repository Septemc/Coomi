from __future__ import annotations

import subprocess

from coomi.tools.shell import bash as bash_module
from coomi.tools.shell import powershell as powershell_module


def _completed(command):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def test_powershell_tool_does_not_inherit_tui_stdin(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return _completed(command)

    monkeypatch.setattr(powershell_module.subprocess, "run", fake_run)
    result = powershell_module.PowerShellTool().run({"command": "Write-Output ok"})

    assert result.success
    assert captured["stdin"] is subprocess.DEVNULL
    if powershell_module.os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_bash_tool_does_not_inherit_tui_stdin(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return _completed(command)

    monkeypatch.setattr(bash_module.subprocess, "run", fake_run)
    result = bash_module.BashTool().run({"command": "echo ok"})

    assert result.success
    assert captured["stdin"] is subprocess.DEVNULL
    if bash_module.os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
