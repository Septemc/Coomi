"""ProviderEditScreen — Provider 编辑表单屏

使用 Textual Input widget 实现真正的表单输入。
Tab 切换字段，Enter 保存，Esc 返回。
"""
from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static
from textual.widgets.select import NoSelection

from ...services.llm.config import (
    ANTHROPIC_MESSAGES,
    OPENAI_COMPATIBLE,
    OPENAI_RESPONSES,
    PROVIDER_TYPE_OPTIONS,
    ConfigManager,
    ProviderConfig,
    PRESET_PROVIDERS,
    TOOL_PROTOCOLS,
    normalize_provider_type,
)

FIELD_DEFS = [
    ("id", "Provider ID", "唯一标识，如 my-provider"),
    ("type", "兼容模式", "OpenAI Compatible"),
    ("tool_protocol", "Tool protocol (auto/native/structured/mimo/disabled)", "auto"),
    ("display", "显示名称", "如 DeepSeek V4"),
    ("api_key", "API Key", "sk-xxx"),
    ("base_url", "Base URL", "https://api.deepseek.com"),
    ("model", "模型名", "如 deepseek-v4-pro"),
    ("fast_model", "快速模型 (可选)", "如 deepseek-v4-flash"),
]

# 必填字段（标签加红色 * 标记，与 action_save 的校验一致）
REQUIRED_FIELDS = {"id", "api_key", "model"}

# 字段分组（分节标题 + 组内字段 key 顺序）；顺序与 FIELD_DEFS 覆盖一致
FIELD_GROUPS: list[tuple[str, list[str]]] = [  # (分节标题, 组内字段 key 顺序)
    ("身份", ["id", "type", "tool_protocol", "display"]),
    ("连接", ["api_key", "base_url"]),
    ("模型", ["model", "fast_model"]),
]

FIELD_HELP = {
    "id": (
        "[bold]Provider ID[/bold]\n本地唯一标识，建议只使用字母、数字和连字符。"
        "编辑时修改 ID 会迁移配置；如果它是当前 Provider，保存后会同步切换到新 ID。"
    ),
    "type": (
        "[bold]兼容模式[/bold]\nOpenAI Compatible：最常用的 Chat Completions 兼容接口；"
        "OpenAI Responses：专门用于 GPT 的 Responses API；"
        "Anthropic Messages：Anthropic Messages 兼容接口。界面只提供这三种模式。"
    ),
    "tool_protocol": (
        "[bold]Tool protocol[/bold]\nauto：自动判断（推荐）；native：服务端原生 tool/function calling；"
        "structured：结构化文本兼容；mimo：MiMo 文本工具格式；disabled：完全禁用工具。"
        "不确定时选择 auto。"
    ),
    "display": "[bold]显示名称[/bold]\n只用于界面显示，不会发送给模型服务。",
    "api_key": (
        "[bold]API Key[/bold]\n服务商密钥，保存在 ~/.coomi/config/providers.json。"
        "不要复制到日志、截图或公开仓库；界面以密码形式隐藏输入。"
    ),
    "base_url": (
        "[bold]Base URL[/bold]\n填写服务商文档给出的 API 根地址。OpenAI-compatible 服务常见以 /v1 结尾，"
        "但也有服务要求不带 /v1；Anthropic-compatible 地址同样以服务商文档为准。"
    ),
    "model": "[bold]模型名[/bold]\n必须使用服务商当前提供的准确模型 ID，而不是营销展示名称。",
    "fast_model": (
        "[bold]快速模型（可选）[/bold]\n用于记忆提取、召回等轻量任务。留空时这些任务使用当前主模型。"
    ),
    "preset": (
        "[bold]Provider 预设[/bold]\n预设只填充接口类型、地址和建议模型；"
        "仍需填写自己的 API Key，并按服务商最新文档核对模型名和 Base URL。"
    ),
}

# 预设选项列表
PRESET_OPTIONS = [
    (f"{data['display']} ({preset_id})", preset_id)
    for preset_id, data in PRESET_PROVIDERS.items()
]


class ProviderEditScreen(ModalScreen[bool]):
    """Provider 编辑表单 — 每个字段一个 Input widget"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, config_mgr: ConfigManager, provider: ProviderConfig | None = None, **kwargs):
        super().__init__(**kwargs)
        self._config_mgr = config_mgr
        self._provider = provider
        self._editing = provider is not None

        if provider:
            self._init_values = {
                "id": provider.id,
                "type": normalize_provider_type(provider.type),
                "tool_protocol": provider.tool_protocol,
                "display": provider.display,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "model": provider.model,
                "fast_model": provider.fast_model or "",
            }
        else:
            self._init_values = {
                "id": "",
                "type": OPENAI_COMPATIBLE,
                "tool_protocol": "auto",
                "display": "",
                "api_key": "",
                "base_url": "",
                "model": "",
                "fast_model": "",
            }

    def compose(self) -> ComposeResult:
        title = "编辑 Provider" if self._editing else "新建 Provider"
        with Container(id="provider-edit-container"):
            yield Static(f"  {title}", id="provider-edit-title")

            # 新建时显示预设选择
            if not self._editing:
                yield Static("  [dim]从预设创建（可选）:[/dim]")
                yield Select(
                    options=[("--- 选择预设 ---", "")] + PRESET_OPTIONS,
                    id="preset-select",
                    allow_blank=True,
                )
                yield Static("")

            field_lookup = {key: (label, hint) for key, label, hint in FIELD_DEFS}
            with Vertical(id="provider-edit-form"):
                for group_title, group_keys in FIELD_GROUPS:
                    yield Static(
                        f"[bold #58a6ff]{group_title}[/bold #58a6ff]",
                        classes="provider-field-group",
                    )
                    for key in group_keys:
                        label, hint = field_lookup[key]
                        value = self._init_values.get(key, "")
                        marker = "[red]*[/red] " if key in REQUIRED_FIELDS else ""
                        yield Static(
                            f"  {marker}[#c9d1d9]{label}:[/#c9d1d9]",
                            classes="provider-field-label",
                        )
                        if key == "type":
                            yield Select(
                                options=[(label, provider_type) for provider_type, label in PROVIDER_TYPE_OPTIONS],
                                value=normalize_provider_type(value),
                                allow_blank=False,
                                id="field-type",
                            )
                        else:
                            yield Input(
                                value=value,
                                placeholder=hint,
                                password=(key == "api_key"),
                                id=f"field-{key}",
                            )
                yield Static("")
                yield Static(
                    "  [dim]Tab 切换字段[/dim]   [dim]·[/dim]   "
                    "[#7ee787]Ctrl+S 保存[/#7ee787]   [dim]·[/dim]   "
                    "[#f0883e]Esc 取消[/#f0883e]   [red]*[/red] [dim]为必填[/dim]"
                )
            yield Static(
                "[bold #d4a72c]字段说明[/bold #d4a72c]",
                id="provider-field-help-title",
            )
            yield Static(FIELD_HELP["preset"], id="provider-field-help")
            yield Static("", id="provider-edit-error")

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget_id = event.widget.id or ""
        if widget_id == "preset-select":
            key = "preset"
        elif widget_id.startswith("field-"):
            key = widget_id.removeprefix("field-")
        else:
            return
        help_text = FIELD_HELP.get(key)
        if help_text:
            self.query_one("#provider-field-help", Static).update(help_text)

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter saves from text fields while Select keeps its native Enter behavior."""
        event.stop()
        self.action_save()

    @on(Select.Changed)
    def on_preset_selected(self, event: Select.Changed) -> None:
        """预设选择变更时自动填充字段"""
        if event.select.id != "preset-select":
            return
        if event.value == "":
            return
        preset_id = event.value
        if preset_id not in PRESET_PROVIDERS:
            return

        preset = PRESET_PROVIDERS[preset_id]
        # 自动填充字段
        fields_to_fill = {
            "id": preset_id,
            "type": normalize_provider_type(preset.get("type", OPENAI_COMPATIBLE)),
            "tool_protocol": preset.get("tool_protocol", "auto"),
            "display": preset.get("display", ""),
            "base_url": preset.get("base_url", ""),
            "model": preset.get("model", ""),
            "fast_model": preset.get("fast_model", ""),
        }
        for key, value in fields_to_fill.items():
            try:
                if key == "type":
                    self.query_one("#field-type", Select).value = value
                else:
                    inp = self.query_one(f"#field-{key}", Input)
                    inp.value = value
            except Exception:
                pass

    def action_save(self) -> None:
        """保存 Provider 配置"""
        self.query_one("#provider-edit-error", Static).update("")
        values = {}
        for key, _, _ in FIELD_DEFS:
            try:
                if key == "type":
                    selected = self.query_one("#field-type", Select).value
                    values[key] = "" if isinstance(selected, NoSelection) else str(selected)
                else:
                    inp = self.query_one(f"#field-{key}", Input)
                    values[key] = inp.value.strip()
            except Exception:
                values[key] = self._init_values.get(key, "")

        # 验证必填字段
        if not values.get("id"):
            self._show_error("Provider ID 不能为空")
            return
        if not values.get("api_key"):
            self._show_error("API Key 不能为空")
            return
        if not values.get("model"):
            self._show_error("模型名不能为空")
            return
        provider_type = normalize_provider_type(values.get("type") or OPENAI_COMPATIBLE)
        if provider_type not in {OPENAI_COMPATIBLE, OPENAI_RESPONSES, ANTHROPIC_MESSAGES}:
            self._show_error(
                "兼容模式只能是 OpenAI Compatible / OpenAI Responses / Anthropic Messages"
            )
            return

        tool_protocol = (values.get("tool_protocol") or "auto").lower().replace("-", "_")
        if tool_protocol not in TOOL_PROTOCOLS:
            self._show_error(
                "Tool protocol 必须是 auto / native / structured / mimo / disabled"
            )
            return

        config = ProviderConfig(
            id=values["id"],
            type=provider_type,
            display=values.get("display") or values["id"],
            api_key=values["api_key"],
            model=values["model"],
            base_url=values.get("base_url", ""),
            fast_model=values.get("fast_model") or None,
            tool_protocol=tool_protocol,
        )
        old_id = self._provider.id if self._provider else None
        was_active = bool(old_id and self._config_mgr.data.get("active") == old_id)
        if old_id and old_id != config.id:
            self._config_mgr.remove_provider(old_id)
        self._config_mgr.add_provider(config)
        if was_active and old_id != config.id:
            self._config_mgr.set_active(config.id)
        self.dismiss(True)

    def _show_error(self, message: str) -> None:
        self.query_one("#provider-edit-error", Static).update(f"[bold red]{message}[/bold red]")

    def action_cancel(self) -> None:
        self.dismiss(False)
