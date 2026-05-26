"""PowerShell 工具 - 执行 PowerShell 命令"""
from __future__ import annotations

import subprocess
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class PowerShellTool(BaseTool):
    """执行 PowerShell 命令"""

    name = "PowerShell"
    description = "Executes a given PowerShell command and returns its output."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The PowerShell command to execute",
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

        try:
            # 使用 powershell.exe 执行命令
            result = subprocess.run(
                ["powershell.exe", "-Command", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"

            if result.returncode != 0:
                return ToolResult(
                    success=False,
                    output=output,
                    error=f"Command exited with code {result.returncode}",
                )

            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout} seconds",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
