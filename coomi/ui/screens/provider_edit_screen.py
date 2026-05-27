"""ProviderEditScreen — Provider 编辑表单屏

使用 Textual Input widget 实现真正的表单输入。
Tab 切换字段，Enter 保存，Esc 返回。
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static
from textual import on

from ...services.llm.config import ConfigManager, ProviderConfig, PRESET_PROVIDERS

FIELD_DEFS = [
    ("id", "Provider ID", "唯一标识，如 my-deepseek"),
    ("type", "类型 (deepseek/openai/anthropic/generic)", "deepseek"),
    ("display", "显示名称", "如 DeepSeek V4"),
    ("api_key", "API Key", "sk-xxx"),
    ("base_url", "Base URL", "https://api.deepseek.com"),
    ("model", "模型名", "如 deepseek-v4-pro"),
    ("fast_model", "快速模型 (可选)", "如 deepseek-v4-flash"),
]

# 预设选项列表
PRESET_OPTIONS = [
    (preset_id, f"{data['display']} ({preset_id})")
    for preset_id, data in PRESET_PROVIDERS.items()
]


class ProviderEditScreen(ModalScreen[bool]):
    """Provider 编辑表单 — 每个字段一个 Input widget"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("enter", "save", "Save", priority=True),
    ]

    def __init__(self, config_mgr: ConfigManager, provider: ProviderConfig | None = None, **kwargs):
        super().__init__(**kwargs)
        self._config_mgr = config_mgr
        self._provider = provider
        self._editing = provider is not None

        if provider:
            self._init_values = {
                "id": provider.id,
                "type": provider.type,
                "display": provider.display,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "model": provider.model,
                "fast_model": provider.fast_model or "",
            }
        else:
            self._init_values = {
                "id": "",
                "type": "deepseek",
                "display": "",
                "api_key": "",
                "base_url": "",
                "model": "",
                "fast_model": "",
            }

    def compose(self) -> ComposeResult:
        title = "⚙ Edit Provider" if self._editing else "⚙ New Provider"
        with Container(id="provider-edit-container"):
            yield Static(f"  {title}", id="provider-edit-title")

            # 新建时显示预设选择
            if not self._editing:
                yield Static("  [dim]从预设创建（可选）:[/dim]")
                yield Select(
                    options=[("", "--- 选择预设 ---")] + PRESET_OPTIONS,
                    id="preset-select",
                    allow_blank=True,
                )
                yield Static("")

            with Vertical(id="provider-edit-form"):
                for key, label, hint in FIELD_DEFS:
                    value = self._init_values.get(key, "")
                    yield Static(f"  [dim]{label}:[/dim]")
                    yield Input(
                        value=value,
                        placeholder=hint,
                        id=f"field-{key}",
                    )
                yield Static("")
                yield Static("  [dim]Tab 切换字段  Ctrl+S 保存  Esc 取消[/dim]")

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """阻止 Input 的 Submitted 事件传播，避免触发 Screen 的 Enter 绑定"""
        event.stop()

    @on(Select.Changed)
    def on_preset_selected(self, event: Select.Changed) -> None:
        """预设选择变更时自动填充字段"""
        if event.value == "":
            return
        preset_id = event.value
        if preset_id not in PRESET_PROVIDERS:
            return

        preset = PRESET_PROVIDERS[preset_id]
        # 自动填充字段
        fields_to_fill = {
            "id": preset_id,
            "type": preset.get("type", "generic"),
            "display": preset.get("display", ""),
            "base_url": preset.get("base_url", ""),
            "model": preset.get("model", ""),
            "fast_model": preset.get("fast_model", ""),
        }
        for key, value in fields_to_fill.items():
            try:
                inp = self.query_one(f"#field-{key}", Input)
                inp.value = value
            except Exception:
                pass

    def action_save(self) -> None:
        """保存 Provider 配置"""
        values = {}
        for key, _, _ in FIELD_DEFS:
            try:
                inp = self.query_one(f"#field-{key}", Input)
                values[key] = inp.value.strip()
            except Exception:
                values[key] = self._init_values.get(key, "")

        # 验证必填字段
        if not values.get("id"):
            self.app._show_command_result("[red]Provider ID 不能为空[/red]")
            return
        if not values.get("api_key"):
            self.app._show_command_result("[red]API Key 不能为空[/red]")
            return
        if not values.get("model"):
            self.app._show_command_result("[red]模型名不能为空[/red]")
            return

        config = ProviderConfig(
            id=values["id"],
            type=values.get("type", "generic") or "generic",
            display=values.get("display") or values["id"],
            api_key=values["api_key"],
            model=values["model"],
            base_url=values.get("base_url", ""),
            fast_model=values.get("fast_model") or None,
        )
        self._config_mgr.add_provider(config)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
