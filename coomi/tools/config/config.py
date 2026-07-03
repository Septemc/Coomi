"""Config 工具 - 获取/设置配置"""
from __future__ import annotations

import os
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class ConfigTool(BaseTool):
    """获取/设置配置"""

    name = "Config"
    description = "Use this skill to configure the Coomi Agent harness via settings.json."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def __init__(self):
        self.config: dict[str, Any] = {}

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "setting": {
                    "type": "string",
                    "description": 'The setting key (e.g., "theme", "model")',
                },
                "value": {
                    "description": "The new value. Omit to get current value.",
                },
            },
            "required": ["setting"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        setting = arguments["setting"]
        value = arguments.get("value")

        try:
            if value is None:
                # 获取配置
                current = self.config.get(setting, os.getenv(setting, "not set"))
                return ToolResult(
                    success=True,
                    output=f"{setting} = {current}",
                )
            else:
                # 设置配置
                self.config[setting] = value
                return ToolResult(
                    success=True,
                    output=f"{setting} set to {value}",
                )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
