from __future__ import annotations

import subprocess

from coomi.tools.shell import bash as bash_module
from coomi.tools.shell import powershell as powershell_module


class _FakePopen:
    """最小 Popen 桩：记录构造参数，模拟一次成功的 communicate。"""

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = 0

    def communicate(self, timeout=None):
        self.returncode = 0
        return ("ok", "")

    def poll(self):
        return self.returncode


def test_powershell_tool_does_not_inherit_tui_stdin(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _FakePopen(command, **kwargs)

    monkeypatch.setattr(powershell_module.subprocess, "Popen", fake_popen)
    result = powershell_module.PowerShellTool().run({"command": "Write-Output ok"})

    assert result.success
    assert captured["stdin"] is subprocess.DEVNULL
    if powershell_module.os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_bash_tool_does_not_inherit_tui_stdin(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _FakePopen(command, **kwargs)

    if bash_module.os.name == "nt":
        monkeypatch.setattr(
            bash_module,
            "_find_windows_bash",
            lambda: r"C:\Program Files\Git\bin\bash.exe",
        )
    monkeypatch.setattr(bash_module.subprocess, "Popen", fake_popen)
    result = bash_module.BashTool().run({"command": "echo ok"})

    assert result.success
    assert captured["stdin"] is subprocess.DEVNULL
    if bash_module.os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert captured["shell"] is False
        assert captured["command"] == [
            r"C:\Program Files\Git\bin\bash.exe",
            "--noprofile",
            "--norc",
            "-c",
            "echo ok",
        ]
    else:
        assert captured["shell"] is True
        assert captured["command"] == "echo ok"


def test_windows_bash_invocation_never_uses_cmd_exe():
    invocation = bash_module._windows_bash_invocation(
        "mkdir -p nested/path",
        r"C:\Program Files\Git\bin\bash.exe",
    )

    assert invocation == [
        r"C:\Program Files\Git\bin\bash.exe",
        "--noprofile",
        "--norc",
        "-c",
        "mkdir -p nested/path",
    ]
    assert bash_module._windows_bash_invocation("mkdir -p nested/path", None) is None


def test_bash_mkdir_p_never_creates_dash_p_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = bash_module.BashTool().run({"command": "mkdir -p nested/path"})

    assert not (tmp_path / "-p").exists()
    if result.success:
        assert (tmp_path / "nested" / "path").is_dir()
    else:
        assert bash_module.os.name == "nt"
        assert "real Bash executable" in (result.error or "")
