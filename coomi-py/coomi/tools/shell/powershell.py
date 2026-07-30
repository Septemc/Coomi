"""PowerShell 工具 - 执行 PowerShell 命令

生产级错误处理：
- 详细的错误信息，包含执行的命令、工作目录、超时设置
- 常见 exit code 诊断提示
- 输出截断保护
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from ...engine.process_registry import PROCESS_REGISTRY
from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult

MAX_OUTPUT_LENGTH = 50000


def _truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """截断过长的输出"""
    if len(output) <= max_length:
        return output
    half = max_length // 2
    return (
        output[:half]
        + f"\n\n... [输出已截断，原始长度 {len(output)} 字符] ...\n\n"
        + output[-half:]
    )


class PowerShellTool(BaseTool):
    """执行 PowerShell 命令"""

    name = "PowerShell"
    description = (
        "Executes a PowerShell command and returns its output. Prefer this tool on "
        "Windows for file operations, Windows paths, cmdlets, and cmd.exe-style tasks."
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
                    "description": "The PowerShell command to execute; preferred on Windows for filesystem and shell tasks",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in milliseconds",
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
        timeout = arguments.get("timeout", 120000) / 1000
        cwd = os.getcwd()
        proc: subprocess.Popen | None = None

        try:
            process_options: dict[str, Any] = {"stdin": subprocess.DEVNULL}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            # 用 Popen 而非 run，以便把句柄登记到 PROCESS_REGISTRY，
            # 让「停止真杀」能终止仍在运行的子进程。
            proc = subprocess.Popen(
                ["powershell.exe", "-Command", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
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
                error_detail = (
                    f"Command exited with code {returncode}\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}\n"
                    f"  Timeout: {timeout}s"
                )
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
                    f"  Hint: 尝试增加超时时间或拆分命令"
                ),
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"PowerShell not found\n"
                    f"  Command: {command}\n"
                    f"  Hint: powershell.exe 未找到，请确认系统已安装 PowerShell"
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
