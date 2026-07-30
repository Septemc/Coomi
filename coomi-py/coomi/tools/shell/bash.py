"""Bash 工具 - 执行 Shell 命令

生产级错误处理：
- 详细的错误信息，包含执行的命令、工作目录、超时设置
- 常见 exit code 的诊断提示
- 输出截断保护
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...engine.process_registry import PROCESS_REGISTRY
from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult

# 常见 exit code 诊断提示
EXIT_CODE_HINTS: dict[int, str] = {
    1: "一般性错误，请检查命令语法和参数是否正确",
    2: "命令用法错误，通常是缺少必要参数或参数格式不对",
    126: "权限不足，尝试 chmod +x 或检查执行权限",
    127: "命令未找到，请检查是否已安装相关工具并加入 PATH",
    128: "退出参数无效",
    130: "命令被 Ctrl+C 中断 (SIGINT)",
    137: "命令被 kill -9 强制终止 (SIGKILL)，可能是内存不足 (OOM)",
    139: "段错误 (SIGSEGV)，程序崩溃",
    141: "管道破裂 (SIGPIPE)，通常是下游进程已退出",
    143: "命令被 SIGTERM 终止",
}

MAX_OUTPUT_LENGTH = 50000  # 输出截断阈值


def _find_windows_bash() -> str | None:
    """Find a native Windows Bash implementation and exclude WSL launchers."""
    candidates: list[Path] = []

    git_executable = shutil.which("git")
    if git_executable:
        git_root = Path(git_executable).resolve().parent.parent
        candidates.extend(
            [
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ]
        )

    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidates.extend(
                [
                    Path(base) / "Git" / "bin" / "bash.exe",
                    Path(base) / "Git" / "usr" / "bin" / "bash.exe",
                ]
            )

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(
            [
                Path(local_app_data) / "Programs" / "Git" / "bin" / "bash.exe",
                Path(local_app_data) / "Programs" / "Git" / "usr" / "bin" / "bash.exe",
            ]
        )

    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry:
            candidates.append(Path(path_entry) / "bash.exe")

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).replace("/", "\\").casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized.endswith("\\windows\\system32\\bash.exe"):
            continue
        if "\\microsoft\\windowsapps\\bash.exe" in normalized:
            continue
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_bash_invocation(command: str, bash_executable: str | None) -> list[str] | None:
    if not bash_executable:
        return None
    return [bash_executable, "--noprofile", "--norc", "-c", command]


def _truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """截断过长的输出"""
    if len(output) <= max_length:
        return output
    half = max_length // 2
    return (
        output[:half]
        + f"\n\n... [输出已截断，原始长度 {len(output)} 字符，截断至 {max_length} 字符] ...\n\n"
        + output[-half:]
    )


class BashTool(BaseTool):
    """执行 Shell 命令"""

    name = "Bash"
    description = (
        "Executes a bash/sh command and returns its output. On Windows this uses a real "
        "Git/MSYS Bash and never cmd.exe; prefer PowerShell for Windows paths, file "
        "operations, cmdlets, and cmd.exe syntax."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The bash/sh command to execute; on Windows it requires Git/MSYS Bash "
                        "and is never interpreted by cmd.exe"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in milliseconds (up to 600000ms / 10 minutes)",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does",
                },
            },
            "required": ["command"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments["command"]
        timeout = arguments.get("timeout", 120000) / 1000  # 转换为秒
        cwd = os.getcwd()
        proc: subprocess.Popen | None = None

        try:
            process_options: dict[str, Any] = {"stdin": subprocess.DEVNULL}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
                invocation = _windows_bash_invocation(command, _find_windows_bash())
                if invocation is None:
                    return ToolResult(
                        success=False,
                        output="",
                        error=(
                            "A real Bash executable is not available on Windows, so the command "
                            "was not run. Install Git for Windows or use the PowerShell tool. "
                            "Coomi will not pass Bash syntax to cmd.exe because flags such as "
                            "'mkdir -p' can otherwise be interpreted as directory names."
                        ),
                    )
                use_shell = False
            else:
                invocation = command
                use_shell = True
            # 用 Popen 而非 run，以便把句柄登记到 PROCESS_REGISTRY，
            # 让「停止真杀」能终止仍在运行的子进程。
            proc = subprocess.Popen(
                invocation,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                **process_options,
            )
            PROCESS_REGISTRY.register(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            finally:
                PROCESS_REGISTRY.unregister(proc)
            returncode = proc.returncode

            output = _truncate_output(stdout or "")
            if stderr:
                stderr_truncated = _truncate_output(stderr)
                output += f"\n[stderr]\n{stderr_truncated}"

            if returncode != 0:
                hint = EXIT_CODE_HINTS.get(returncode, "")
                error_parts = [
                    f"Command exited with code {returncode}",
                    f"  Command: {command}",
                    f"  Working directory: {cwd}",
                    f"  Timeout: {timeout}s",
                ]
                if hint:
                    error_parts.append(f"  Hint: {hint}")
                error_detail = "\n".join(error_parts)
                return ToolResult(
                    success=False,
                    output=output,
                    error=error_detail,
                )

            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            # communicate 超时后子进程仍在运行，必须主动收尾避免僵尸/泄漏。
            if proc is not None:
                try:
                    proc.kill()
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command timed out after {timeout} seconds\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}\n"
                    f"  Hint: 命令执行时间过长，可以尝试：\n"
                    f"    1. 增加超时时间 (timeout 参数)\n"
                    f"    2. 将命令拆分为更小的步骤\n"
                    f"    3. 在后台运行命令 (添加 & 或使用 nohup)"
                ),
            )
        except FileNotFoundError as e:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command not found: {e}\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}\n"
                    f"  Hint: 请检查命令是否拼写正确，相关工具是否已安装并加入 PATH"
                ),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command execution failed: {type(e).__name__}: {e}\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}"
                ),
            )
