# Phase 3: 多面板布局 & 导航系统

> **状态**: 待执行 | **优先级**: P2（结构级 UX 改进） | **预计工作量**: 3-4 天 | **前置依赖**: Phase 2
>
> **目标**: 从"单列聊天框"升级为"专业 IDE 风格的终端工作台"：
>
> - Screen 架构（MainScreen + 模态 CommandPalette）
> - 纯瀑布流布局（无侧栏，工具调用以 Banner 嵌入消息流）
> - Plan Mode 切换（进入时可见 Plan 模式提示，退出时自动 / 手动）
> - Plan 内容在 RichLog 中展示
> - Shift+Enter 换行输入（TextArea 替代 Input）
> - 纯黑背景 + 鼠标直接选中文本（不按 Shift）
> - Header 栏（模型名 | 快捷键提示）+ 命令面板（Ctrl+P）

---

## 前置背景

### 当前布局

```
Screen (vertical)
  RichLog (#message-log)      height: 1fr
  StreamPreview (#stream)      height: auto
  StatusPanel (#status-panel) height: 2
  Input (#prompt-input)       dock: bottom, height: 3
```

纯线性聊天界面，缺乏模态叠加、换行输入、Plan Mode 支持。

### Phase 3 要交付什么

| 模块                      | 说明                                                                                     | 关键文件                                                   |
| ------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **3.1 Screen 架构** | CoomiApp 降级为 Provider 管理器，所有 UI 委托给 MainScreen；CommandPalette 为模态 Screen | `screens/main_screen.py`, `screens/command_palette.py` |
| **3.2 Plan Mode**   | 进入 Plan 时 StatusPanel 显示 "Plan Mode"，Plan 内容渲染至 RichLog；退出时恢复           | `textual_app.py`, `screens/main_screen.py`             |
| **3.3 换行输入**    | TextArea 替代 Input，Shift+Enter 提交，Enter 换行                                        | `textual_app.py` compose, `coomi.tcss`                 |
| **3.4 CSS & 配色**  | 纯黑背景 `#000000`，鼠标选中无需 Shift（`highlight: true`）                          | `coomi.tcss`                                             |
| **3.5 快捷键体系**  | Header + 命令面板 Ctrl+P，集中式 BINDINGS                                                | `keybindings.py`, `screens/command_palette.py`         |

---

## 3.1 Screen 架构迁移

### 3.1.1 新布局

```
┌──────────────────────────────────────────────────┐
│ Coomi Agent | deepseek-v4-pro    Esc:P cancel     │ ← Header (height: 1, dock: top)
│ Ctrl+P:cmd  Ctrl+L:clear  F1:help                │
├──────────────────────────────────────────────────┤
│                                                    │
│ You: 分析这段代码...                            │
│                                                    │
│ [Thinking (2.3s)]                             │
│  │ 用户想让我分析代码结构...                        │
│                                                    │
│ ReadTool (file_path: main.py)   [Done: 0.3s]  │ ← ToolCallBanner (RichLog 内)
│  ─────────────────────────────────                 │
│  def main():                                       │
│      app = CoomiApp()                              │
│      app.run()                                     │
│                                                    │
│ 这是一个简单的入口文件...                       │
│                                                    │
│  ── [流式输出中...] ──                             │
│                                                    │
├──────────────────────────────────────────────────┤
│ Plan Mode    ·································  │ ← StatusPanel (height: 2)
│ ◎ Ready                           Esc to cancel   │
├──────────────────────────────────────────────────┤
│ 用户输入... (Shift+Enter 换行, Enter 发送)        │ ← TextArea (dock: bottom)
└──────────────────────────────────────────────────┘
```

**设计原则**:

- 对话流是**纵向时间线叙事**，工具调用是对话的一部分，以 ToolCallBanner 嵌入 RichLog
- 无侧栏、无水平分栏，保持 Claude Code 的简洁 terminal 体验
- 纯黑背景 `#000000`，突出内容而非界面框架

### 3.1.2 新建 `screens/__init__.py`

```python
"""UI Screens"""
```

### 3.1.3 新建 `screens/main_screen.py`

```python
"""MainScreen — 主应用界面"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, RichLog, Static, TextArea

from ..widgets.status_panel import StatusPanel


class MainScreen(Screen):
    """Coomi Agent 主界面。

    布局:
      Header         — 模型名 | 快捷键提示 (dock: top)
      RichLog        — 对话日志 + ToolCallBanner 瀑布流 (1fr)
      StreamPreview  — 流式输出预览 (height: auto)
      StatusPanel    — 2 行状态栏 (height: 2)
      TextArea       — 多行输入框 (dock: bottom, height: 3)
    """

    BINDINGS = [
        ("escape", "cancel_or_exit", "Cancel / Exit"),
        ("ctrl+p", "command_palette", "Commands"),
        ("ctrl+l", "clear_screen", "Clear"),
        ("ctrl+r", "toggle_reasoning", "Toggle reasoning"),
        ("ctrl+s", "save_session", "Save"),
        ("ctrl+o", "load_session", "Load"),
        ("f1", "show_help", "Help"),
    ]

    def __init__(self, app_context: dict):
        super().__init__()
        self._ctx = app_context

    def compose(self) -> ComposeResult:
        yield Header(id="app-header", show_clock=False)
        yield RichLog(id="message-log", markup=True, wrap=True, highlight=True)
        yield Static(id="stream-preview")
        yield StatusPanel(self._ctx["status_line"], id="status-panel")
        yield TextArea(
            id="prompt-input",
            text="",
            placeholder="> 输入消息 (Shift+Enter 换行, Enter 发送)",
        )

    def on_mount(self) -> None:
        header = self.query_one("#app-header", Header)
        display = self._ctx.get("display_name", "Coomi")
        header.text = f"Coomi Agent | {display}"

    # --- Actions ---

    def action_cancel_or_exit(self) -> None:
        self.app.action_cancel_or_exit()

    def action_command_palette(self) -> None:
        self.app.push_screen("command_palette")

    def action_clear_screen(self) -> None:
        self.app._handle_clear()

    # --- 便捷属性 ---

    @property
    def message_log(self) -> RichLog:
        return self.query_one("#message-log", RichLog)

    @property
    def stream_preview(self) -> Static:
        return self.query_one("#stream-preview", Static)

    @property
    def status_panel(self) -> StatusPanel:
        return self.query_one("#status-panel", StatusPanel)

    @property
    def prompt_input(self) -> TextArea:
        return self.query_one("#prompt-input", TextArea)
```

### 3.1.4 修改 `textual_app.py`

**核心变更**:

1. `compose()` 保留 RichLog + StreamPreview（不在 MainScreen 中重新定义）
2. `on_mount()` 末尾推送 `MainScreen`
3. Input 替换为 TextArea
4. 添加 Plan Mode 支持字段和方法
5. `_wl_to_log()` 改为 `self.screen.query_one` 跨 Screen 访问

```python
# textual_app.py — 关键修改部分

def compose(self) -> ComposeResult:
    """仅产出瀑布流控件，Header/StatusPanel/Input 由 MainScreen 管理"""
    yield RichLog(id="message-log", markup=True, wrap=True, highlight=True)
    yield Static(id="stream-preview")

def on_mount(self) -> None:
    # ... 现有初始化代码不变 ...
    # 最后推送 MainScreen
    self._plan_mode = False  # Plan Mode 标志
    self.push_screen(MainScreen(self._ctx))

    # 欢迎信息
    tool_count = len(self._tool_registry.list_tools())
    self._wl_to_log(
        f"[bold cyan]Coomi Agent[/bold cyan] "
        f"[dim]({self._display_name}, {tool_count} tools)[/dim]"
    )
    self._wl_to_log(
        "[dim]Commands: /model | /context | /memory | /clear | exit[/dim]"
    )
    self._wl_to_log("[dim]Shortcuts: Ctrl+P Commands | Ctrl+L Clear | Esc Cancel[/dim]")

def _wl_to_log(self, content: str | object) -> None:
    """跨 Screen 写入消息日志。"""
    try:
        log = self.screen.query_one("#message-log", RichLog)
        log.write(content)
    except Exception:
        pass  # Screen 尚未挂载时忽略
```

**`_run_agent()` 的 widget 查询调整**:

```python
# 修改前
log = self.query_one("#message-log", RichLog)

# 修改后
screen = self.screen
log = screen.query_one("#message-log", RichLog)
status = screen.query_one("#status-panel", StatusPanel)
preview = screen.query_one("#stream-preview", Static)
prompt = screen.query_one("#prompt-input", TextArea)
```

### 3.1.5 新建 `screens/command_palette.py`

```python
"""CommandPalette — 模态命令面板 (Ctrl+P)"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, ListView, ListItem, Label


COMMANDS: list[dict] = [
    {"id": "model", "label": "Switch Model", "action": "/model"},
    {"id": "context", "label": "Set Context Window", "action": "/context"},
    {"id": "memory", "label": "Memory Commands", "action": "/memory"},
    {"id": "clear", "label": "Clear Screen", "action": "/clear"},
    {"id": "save", "label": "Save Session", "action": None, "key": "ctrl+s"},
    {"id": "load", "label": "Load Session", "action": None, "key": "ctrl+o"},
    {"id": "plan", "label": "Toggle Plan Mode", "action": "/plan"},
    {"id": "exit", "label": "Exit Coomi", "action": "exit"},
]


class CommandPalette(ModalScreen):
    """模态命令面板，支持模糊搜索。"""

    def compose(self) -> ComposeResult:
        yield Input(id="palette-input", placeholder="Search commands...")
        yield ListView(id="palette-list", *[
            ListItem(Label(f"  {c['label']}  [{c.get('key', c.get('action', ''))}]"))
            for c in COMMANDS
        ])

    def on_input_changed(self, event: Input.Changed) -> None:
        """模糊过滤命令列表。"""
        query = event.value.lower()
        lv = self.query_one("#palette-list", ListView)
        lv.clear()
        for c in COMMANDS:
            if query in c["label"].lower() or query in c["id"]:
                lv.append(ListItem(Label(f"  {c['label']}  [{c.get('key', c.get('action', ''))}]")))

    def on_list_view_selected(self, event) -> None:
        """选中命令后执行。"""
        idx = self.query_one("#palette-list", ListView).index
        cmd = COMMANDS[idx]
        self.dismiss(cmd)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
```

---

## 3.2 Plan Mode — 导航式多问题问询

Plan Mode 下 Agent 通过 `AskUserQuestionTool` 向用户发起**多问题问询**，采用**顶部导航栏 + 详情区**的布局设计。

### 3.2.1 问询布局简图

当 Agent 调用 AskUserQuestionTool 时，在消息流中渲染**导航式问询面板**：

```
┌──────────────────────────────────────────────────────┐
│  ┌──────────────────────────────────────────────────┐ │
│  │ 用户群体 ▎ 功能优先级 ▎ 时间约束 ▎ 技术栈       │ │ ← 横向导航栏
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ Q1: 项目的目标用户群体是哪些？ ────────────────┐ │
│  │                                                    │ │
│  │  推荐: 企业团队（B端）—— 匹配项目定位            │ │ ← 推荐建议
│  │                                                    │ │
│  │  ○ 个人开发者（C端）                               │ │
│  │  ● 企业团队（B端）  ← recommended                  │ │ ← 焦点高亮
│  │  ○ 两者都有                                        │ │
│  │  ○ Other: ________________________________        │ │ ← 自定义输入
│  │                                                    │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  [ ←→ 切换问题   ↑↓ 选择选项   Enter 确认   Esc 取消 ] │ ← 操作提示
└──────────────────────────────────────────────────────────┘
```

### 3.2.2 交互模型

| 按键 | 行为 |
|------|------|
| **← / →** | 切换问题（导航栏高亮移动） |
| **Tab / Shift+Tab** | 同 ← →，切换问题 |
| **↑ / ↓** | 在当前问题的选项列表中移动高亮 |
| **Enter** | 确认所有问题的选择，提交答案 |
| **字母/数字** | 当焦点在 "Other: ___" 输入框时，输入自定义内容 |
| **Esc** | 取消整个问询（返回 Agent 一个取消信号） |
| **最后一个选项始终为 "Other"** | 选中后用户可输入自由文本 |

### 3.2.3 实现方案

**新建文件**: `widgets/plan_panel.py`

```python
"""PlanNavBar + QuestionDetail + PlanPanel — 导航式多问题问询"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input
from textual.widget import Widget
from rich.table import Table
from rich.text import Text


class PlanNavBar(Widget):
    """横向导航栏 — 显示各问题的简写标题（≤4字符）。

    布局示例:
      ┌────────────────────────────────────────┐
      │ 用户群体 ▎ 功能优先级 ▎ 时间约束 ▎ 技术栈 │
      └────────────────────────────────────────┘
    """

    def __init__(self, headers: list[str]):
        super().__init__()
        self._headers = headers
        self._active_idx = 0

    def render(self):
        table = Table.grid(padding=(0, 1))
        row = []
        for i, header in enumerate(self._headers):
            if i == self._active_idx:
                row.append(f"[bold reverse]{header}[/bold reverse]")
            else:
                row.append(f"[dim]{header}[/dim]")
        table.add_row(*row)
        return table

    @property
    def active_index(self) -> int:
        return self._active_idx

    def set_active(self, idx: int) -> None:
        self._active_idx = idx
        self.refresh()

    def move_left(self) -> int:
        self._active_idx = (self._active_idx - 1) % len(self._headers)
        self.refresh()
        return self._active_idx

    def move_right(self) -> int:
        self._active_idx = (self._active_idx + 1) % len(self._headers)
        self.refresh()
        return self._active_idx


class QuestionOption(Widget):
    """单个选项 — 单选按钮样式。"""

    def __init__(self, label: str, value: str, is_other: bool = False, is_recommended: bool = False):
        super().__init__()
        self.label = label
        self.value = value
        self.is_other = is_other
        self.is_recommended = is_recommended
        self.selected = False
        self.other_text = "" if is_other else None

    def render(self):
        marker = "[bold cyan]●[/bold cyan]" if self.selected else "○"
        if self.is_other:
            input_display = self.other_text or "..."
            return f"  {marker} Other: [dim]{input_display}[/dim]"
        label_text = f"[bold]{self.label}[/bold]" if self.selected else self.label
        rec_tag = "  [dim]<- recommended[/dim]" if self.is_recommended else ""
        return f"  {marker} {label_text}{rec_tag}"


class QuestionDetail(Vertical):
    """单个问题详情 — 标题 + 推荐建议 + 选项列表。

    布局:
      ┌─ Q1: 项目的目标用户群体是哪些？ ──────────┐
      │  推荐: 企业团队（B端）—— 匹配项目定位      │
      │                                              │
      │  ○ 个人开发者（C端）                         │
      │  ● 企业团队（B端）  ← recommended            │
      │  ○ 两者都有                                  │
      │  ○ Other: ________________________________  │
      └──────────────────────────────────────────────┘
    """

    def __init__(self, question: str, options: list[dict],
                 recommendation: str | None = None, block_idx: int = 0):
        super().__init__()
        self.question = question
        self.block_idx = block_idx
        self.recommendation = recommendation
        self._focus_idx = 0

        self.options = list(options)
        # 自动追加 Other 选项
        self.options.append({"label": "Other", "value": "__other__", "is_other": True})

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]Q{self.block_idx + 1}: {self.question}[/bold]",
                      classes="question-title")
        if self.recommendation:
            yield Static(f"  [dim]推荐: {self.recommendation}[/dim]",
                          classes="question-recommendation")
        for opt in self.options:
            yield QuestionOption(
                label=opt["label"],
                value=opt["value"],
                is_other=opt.get("is_other", False),
                is_recommended=opt.get("is_recommended", False),
            )

    @property
    def option_count(self) -> int:
        return len(self.query(QuestionOption).nodes)

    @property
    def focused_option(self) -> QuestionOption:
        return self.query(QuestionOption).nodes[self._focus_idx]

    def focus(self) -> None:
        self.focused_option.selected = True
        self.refresh()

    def unfocus(self) -> None:
        self.focused_option.selected = False
        self.refresh()

    def move_up(self) -> None:
        self.focused_option.selected = False
        self._focus_idx = (self._focus_idx - 1) % self.option_count
        self.focused_option.selected = True
        self.refresh()

    def move_down(self) -> None:
        self.focused_option.selected = False
        self._focus_idx = (self._focus_idx + 1) % self.option_count
        self.focused_option.selected = True
        self.refresh()

    def get_selected_value(self) -> dict:
        opt = self.focused_option
        return {
            "option": opt.value,
            "label": opt.label,
            "other_text": opt.other_text if opt.is_other else None,
        }


class PlanPanel(Vertical):
    """导航式多问题问询面板 — 顶部导航栏 + 详情区。

    布局:
      ┌────────────────────────────────────────┐
      │ 用户群体 ▎ 功能优先级 ▎ 时间约束 ▎ 技术栈 │  ← PlanNavBar
      ├────────────────────────────────────────┤
      │  Q1: ...                                │  ← QuestionDetail (活跃)
      │  推荐: ...                               │
      │  ○ ...  ● ...                            │
      │  ○ Other: ___                            │
      ├────────────────────────────────────────┤
      │  [←→ 切换问题  ↑↓ 选选项  Enter 确认]    │  ← 操作提示
      └────────────────────────────────────────┘
    """

    def __init__(self, questions: list[dict]):
        """
        questions: [
            {
                "header": "用户群体",          # 导航栏简写标题（≤4字符）
                "question": "问题完整描述...",
                "recommendation": "推荐选项描述...",  # 可选
                "options": [
                    {"label": "选项A", "value": "a"},
                    {"label": "选项B", "value": "b", "is_recommended": True},
                ],
            },
            ...
        ]
        """
        super().__init__()
        self._questions_data = questions
        self._active_idx = 0

    def compose(self) -> ComposeResult:
        headers = [q.get("header", f"Q{i+1}") for i, q in enumerate(self._questions_data)]
        yield PlanNavBar(headers=headers)
        yield Vertical(id="plan-detail-area")

    def on_mount(self) -> None:
        self._show_detail(0)

    def _show_detail(self, idx: int) -> None:
        detail_area = self.query_one("#plan-detail-area", Vertical)
        detail_area.remove_children()
        q = self._questions_data[idx]
        detail = QuestionDetail(
            question=q["question"],
            options=q.get("options", []),
            recommendation=q.get("recommendation"),
            block_idx=idx,
        )
        self.mount(detail, detail_area)
        detail.focus()
        self._active_idx = idx

    @property
    def active_block(self) -> QuestionDetail:
        return self.query_one(QuestionDetail)

    @property
    def block_count(self) -> int:
        return len(self._questions_data)

    def next_block(self) -> None:
        self.active_block.unfocus()
        new_idx = (self._active_idx + 1) % self.block_count
        self.query_one(PlanNavBar).move_right()
        self._show_detail(new_idx)

    def prev_block(self) -> None:
        self.active_block.unfocus()
        new_idx = (self._active_idx - 1) % self.block_count
        self.query_one(PlanNavBar).move_left()
        self._show_detail(new_idx)

    def get_all_answers(self) -> dict[int, dict]:
        """收集所有问题的答案。"""
        answers = {}
        # 由于只显示一个 QuestionDetail，需要从 _questions_data 重建完整答案
        for i, q in enumerate(self._questions_data):
            if i == self._active_idx:
                answers[i] = self.active_block.get_selected_value()
            else:
                answers[i] = {"option": None, "label": None, "other_text": None}
        return answers
```

### 3.2.4 问询触发与键盘事件分发

**新建文件**: `widgets/command_autocomplete.py`

```python
"""CommandAutocomplete — / 前缀命令自动补全"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static
from textual.widget import Widget


COMMANDS = {
    "/plan": "进入 Plan Mode，发起需求澄清问询",
    "/exit_plan": "退出 Plan Mode",
    "/compact": "立即压缩上下文",
    "/clear": "清空当前会话历史",
    "/model": "切换 LLM 模型",
    "/help": "显示帮助信息",
}


class CommandAutocomplete(Widget):
    """命令自动补全面板 — 当输入以 / 开头时显示。

    布局:
      ┌──────────────────────────────────────┐
      │  /plan        进入 Plan Mode...      │
      │  /exit_plan   退出 Plan Mode         │
      │  /compact      立即压缩上下文        │
      │  /clear        清空当前会话历史      │
      │  /model        切换 LLM 模型         │
      │  /help         显示帮助信息          │
      └──────────────────────────────────────┘
    """

    def __init__(self):
        super().__init__()
        self._query = ""
        self._selected_idx = 0
        self._filtered: list[tuple[str, str]] = []

    def on_mount(self) -> None:
        self._update_filter("")

    def render(self):
        if not self._filtered:
            return "[dim]无匹配命令[/dim]"
        table = Table.grid(padding=(0, 2))
        for i, (cmd, desc) in enumerate(self._filtered):
            if i == self._selected_idx:
                table.add_row(
                    f"[bold reverse]{cmd}[/bold reverse]",
                    f"[dim]{desc}[/dim]",
                )
            else:
                table.add_row(
                    f"[cyan]{cmd}[/cyan]",
                    f"[dim]{desc}[/dim]",
                )
        return table

    def _update_filter(self, query: str) -> None:
        self._query = query
        self._filtered = [
            (cmd, desc) for cmd, desc in COMMANDS.items()
            if cmd.startswith(query)
        ]
        self._selected_idx = 0
        self.refresh()

    def on_input_changed(self, event) -> None:
        if event.value.startswith("/"):
            self._update_filter(event.value)
            self.display = True
        else:
            self.display = False

    def move_up(self) -> None:
        if self._filtered:
            self._selected_idx = (self._selected_idx - 1) % len(self._filtered)
            self.refresh()

    def move_down(self) -> None:
        if self._filtered:
            self._selected_idx = (self._selected_idx + 1) % len(self._filtered)
            self.refresh()

    def get_selected_command(self) -> str | None:
        if self._filtered:
            return self._filtered[self._selected_idx][0]
        return None
```

**`textual_app.py`** 中处理问询：

```python
# CoomiApp 新增属性和方法

class CoomiApp(App):
    def __init__(self, ...):
        super().__init__(...)
        self._plan_mode: bool = False
        self._question_mode: bool = False
        self._plan_panel: PlanPanel | None = None
        self._question_future: asyncio.Future | None = None
        self._autocomplete: CommandAutocomplete | None = None

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    def set_plan_mode(self, active: bool) -> None:
        self._plan_mode = active
        try:
            panel = self.screen.query_one("#status-panel", StatusPanel)
            panel.set_plan_mode(active)
        except Exception:
            pass

    async def _handle_ask_questions(self, questions: list[dict]) -> dict:
        """处理 Agent 发起的多问题问询。

        questions: [
            {
                "header": "用户群体",
                "question": "完整问题描述...",
                "recommendation": "推荐: 选项A",
                "options": [{"label": "选项A", "value": "a"}, ...]
            },
            ...
        ]

        返回: {0: {"option": "a", "label": "选项A", "other_text": None}, ...}
        """
        from .widgets.plan_panel import PlanPanel

        # 1. 渲染问询面板到 RichLog
        panel = PlanPanel(questions)
        log = self.screen.query_one("#message-log", RichLog)
        log.write(panel)

        # 2. 进入问询模式
        self._question_mode = True
        self._plan_panel = panel

        # 3. 更新状态栏
        status = self.screen.query_one("#status-panel", StatusPanel)
        status.set_question_mode()

        # 4. 阻塞等待用户完成
        self._question_future = asyncio.get_event_loop().create_future()
        result = await self._question_future

        # 5. 恢复
        self._question_mode = False
        self._plan_panel = None
        status.set_idle()
        return result

    def on_key(self, event) -> None:
        """全局键盘事件 — 问询模式下拦截方向键/Tab/Enter。"""
        if self._question_mode and self._plan_panel:
            panel = self._plan_panel

            if event.key == "up":
                panel.active_block.move_up()
            elif event.key == "down":
                panel.active_block.move_down()
            elif event.key in ("tab", "right"):
                panel.next_block()
            elif event.key in ("shift+tab", "left"):
                panel.prev_block()
            elif event.key == "enter":
                answers = panel.get_all_answers()
                self._question_future.set_result(answers)
            elif event.key == "escape":
                self._question_future.set_result({"__cancelled__": True})
            else:
                return  # 不 stop 事件，让 Other 输入框接收字符
            event.stop()
```

### 3.2.5 StatusPanel 更新

```python
# StatusPanel 新增方法

class StatusPanel(Widget):
    def __init__(self, ...):
        ...
        self._plan_mode: bool = False
        self._question_mode: bool = False

    def set_plan_mode(self, active: bool) -> None:
        self._plan_mode = active
        self.refresh()

    def set_question_mode(self) -> None:
        self._question_mode = True
        self.refresh()

    def render(self):
        # ... 现有逻辑 ...
        if self._question_mode:
            bottom = (
                "[bold yellow]◎ 等待用户回答...[/bold yellow] "
                "[dim]| ←→ 切换问题  ↑↓ 选选项  Enter 确认  Esc 取消[/dim]"
            )
        elif self._plan_mode:
            bottom = (
                "[bold yellow]⚡ Plan Mode[/bold yellow] "
                "[dim]| Esc to exit plan[/dim]"
            )
```

### 3.2.6 `/` 命令自动补全与 `/plan` 处理

当用户在输入框输入 `/` 时，显示命令自动补全面板：

**`textual_app.py`** 中处理：

```python
# CoomiApp — 输入框事件处理

async def _on_input_changed(self, event) -> None:
    """输入框内容变化时，检查是否需要显示命令补全。"""
    text = event.value
    if text.startswith("/"):
        if self._autocomplete is None:
            from .widgets.command_autocomplete import CommandAutocomplete
            self._autocomplete = CommandAutocomplete()
            # 挂载到输入框上方
            await self.mount(self._autocomplete, after=self.query_one("#prompt-input"))
        self._autocomplete.on_input_changed(event)
    else:
        if self._autocomplete is not None:
            self._autocomplete.display = False

def _on_key_autocomplete(self, event) -> None:
    """命令补全模式下的键盘事件。"""
    if self._autocomplete is None or not self._autocomplete.display:
        return

    if event.key == "up":
        self._autocomplete.move_up()
        event.stop()
    elif event.key == "down":
        self._autocomplete.move_down()
        event.stop()
    elif event.key == "enter":
        cmd = self._autocomplete.get_selected_command()
        if cmd:
            # 替换输入框内容为选中的命令
            self.query_one("#prompt-input").value = cmd
            self._autocomplete.display = False
        event.stop()
    elif event.key == "escape":
        self._autocomplete.display = False
        event.stop()

# _on_submit 中处理 /plan 命令
async def _on_submit(self, text: str) -> None:
    text = text.strip()
    if text == "/plan":
        self.set_plan_mode(True)
        self._wl_to_log("[bold yellow]⚡ Plan Mode activated[/bold yellow]")
        if self._agent:
            self._agent.set_plan_mode(True)
        return
    if text == "/exit_plan":
        self.set_plan_mode(False)
        self._wl_to_log("[dim]Plan Mode deactivated[/dim]")
        if self._agent:
            self._agent.set_plan_mode(False)
        return
    if text == "/compact":
        # 触发上下文压缩
        self._wl_to_log("[dim]压缩上下文...[/dim]")
        return
    if text == "/clear":
        # 清空会话历史
        self._wl_to_log("[dim]会话已清空[/dim]")
        return
    # ... 继续原有提交逻辑
```

---

## 3.3 换行输入 (Shift+Enter)

### 设计

| 按键                  | 行为                                    |
| --------------------- | --------------------------------------- |
| **Enter**       | 提交消息（调用 `on_input_submitted`） |
| **Shift+Enter** | 插入换行                                |
| **Ctrl+Enter**  | 同 Shift+Enter（兼容方案）              |

### 实现

用 `TextArea` 替代 `Input`，通过 `on_text_area_key_pressed` 拦截 Enter：

```python
# textual_app.py — compose 中
from textual.widgets import TextArea

def compose(self):
    yield TextArea(
        id="prompt-input",
        text="",
        placeholder="> 输入消息 (Shift+Enter 换行, Enter 发送)",
    )

# MainScreen 或 CoomiApp 中
def on_text_area_key_pressed(self, event) -> None:
    """拦截 Enter 提交，Shift+Enter 换行。"""
    if event.key == "enter" and not event.shift:
        event.stop()
        ta = self.query_one("#prompt-input", TextArea)
        text = ta.text.strip()
        if text:
            self._on_submit(text)
        ta.text = ""
```

### 处理 `/plan` 命令

当用户在 Plan Mode 下输入 `/plan content`，Agent 接收并执行 EnterPlanModeTool。当输入 `/exit_plan`，执行 ExitPlanModeTool。

`on_input_submitted` 中新增：

```python
async def _on_submit(self, text: str) -> None:
    text = text.strip()
    if text == "/plan":
        self.set_plan_mode(True)
        self._wl_to_log("[bold yellow]⚡ Plan Mode activated[/bold yellow]")
        # 通知 Agent 进入 Plan Mode
        if self._agent:
            self._agent.set_plan_mode(True)
        return
    if text == "/exit_plan":
        self.set_plan_mode(False)
        self._wl_to_log("[dim]Plan Mode deactivated[/dim]")
        if self._agent:
            self._agent.set_plan_mode(False)
        return
    # ... 继续原有提交逻辑
```

---

## 3.4 CSS & 配色

**文件**: `tcss/coomi.tcss`

```css
/* ===== Phase 3: 纯黑瀑布流布局 ===== */

Screen {
    layout: vertical;
    background: #000000;
}

/* Header */
#app-header {
    height: 1;
    dock: top;
    padding: 0 1;
    background: #0d1117;
    color: #8b949e;
    text-style: bold;
}

/* 消息日志 — 主区域，纯黑背景 */
#message-log {
    height: 1fr;
    padding: 0 1;
    background: #000000;
    color: #e6edf3;
}

/* 流式预览 */
#stream-preview {
    height: auto;
    max-height: 10;
    padding: 0 1;
    background: #000000;
    color: #c9d1d9;
    overflow-y: auto;
}

/* 状态面板 */
#status-panel {
    height: 2;
    padding: 0 1;
    background: #0d1117;
}

/* 多行输入框 */
#prompt-input {
    dock: bottom;
    height: 3;
    padding: 0 1;
    background: #0d1117;
    color: #e6edf3;
    border: none;
}

#prompt-input:focus {
    background: #0d1117;
}

/* 命令面板 — 模态叠加层 */
CommandPalette {
    align: center middle;
    background: rgba(0, 0, 0, 0.7);
}

CommandPalette > Vertical {
    width: 60%;
    max-width: 80;
    height: auto;
    max-height: 60%;
    background: #0d1117;
    border: solid #30363d;
    padding: 1;
}

#palette-input {
    dock: top;
    margin: 1 0;
    background: #1c2333;
}

#palette-list {
    height: 1fr;
    overflow-y: auto;
    background: #0d1117;
}

/* 所有 RichLog 内容高亮可选中（无需 Shift） */
RichLog {
    highlight: true;
}
```

### 纯黑背景说明

| 属性                         | 值          | 原因                                              |
| ---------------------------- | ----------- | ------------------------------------------------- |
| `Screen.background`        | `#000000` | 终端 CLI 风格，无 UI 框架感                       |
| `#message-log.background`  | `#000000` | 消息区纯黑，不干扰内容                            |
| `#status-panel.background` | `#0d1117` | 状态栏微灰区分，非纯黑                            |
| `highlight: true`          | 最顶层      | Textual 的 RichLog 支持鼠标选中文本，无需按 Shift |
| `#prompt-input`            | `#0d1117` | 输入框深灰，与纯黑区分                            |

---

## 3.5 快捷键体系

### 集中管理

**新建文件**: `keybindings.py`

```python
"""集中式快捷键管理"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Keybinding:
    keys: str
    description: str
    show_in_header: bool = True


KEYBINDINGS: list[Keybinding] = [
    Keybinding("Esc", "Cancel / Exit", show_in_header=True),
    Keybinding("Ctrl+P", "Commands", show_in_header=True),
    Keybinding("Ctrl+L", "Clear", show_in_header=True),
    Keybinding("Ctrl+R", "Toggle reasoning", show_in_header=False),
    Keybinding("Ctrl+S", "Save session", show_in_header=False),
    Keybinding("Ctrl+O", "Load session", show_in_header=False),
    Keybinding("F1", "Help", show_in_header=True),
]


def format_header_hint() -> str:
    """生成 Header 行显示的快捷键提示。"""
    visible = [k for k in KEYBINDINGS if k.show_in_header]
    return " | ".join(f"{k.keys}:{k.description}" for k in visible)
```

### BINDINGS 注册位置

| Screen             | 注册的快捷键                                    |
| ------------------ | ----------------------------------------------- |
| `MainScreen`     | Esc, Ctrl+P, Ctrl+L, Ctrl+R, Ctrl+S, Ctrl+O, F1 |
| `CommandPalette` | Esc (关闭), Enter (选中)                        |

---

## 实现顺序

```
Week 1:
  └─ 3.1 Screen 架构 → 新建 MainScreen + 修改 textual_app.py （核心改动，必须先做）
  └─ 3.4 CSS 配色 → 纯黑背景 + RichLog highlight

Week 2:
  └─ 3.3 换行输入 → TextArea 替换 Input + Shift+Enter 提交
  └─ 3.5 快捷键体系 → keybindings.py + CommandPalette

Week 3:
  └─ 3.2 Plan Mode → StatusPanel 标记 + /plan /exit_plan 命令
  └─ 端到端验证 + Bug 修复
```

---

## 涉及文件清单

| 操作           | 文件                                                     |
| -------------- | -------------------------------------------------------- |
| **新建** | `screens/__init__.py`                                  |
| **新建** | `screens/main_screen.py`                               |
| **新建** | `screens/command_palette.py`                           |
| **新建** | `keybindings.py`                                       |
| **新建** | `widgets/question_panel.py` — 多问题问询面板                      |
| **修改** | `textual_app.py` — Screen 架构 + TextArea + Plan Mode + AskQuestion |
| **修改** | `tcss/coomi.tcss` — 纯黑背景 + 多行输入 + 选中                      |
| **修改** | `status_panel.py` — Plan Mode + Question Mode 指示                 |

---

## 验证标准

- [ ] `MainScreen` 正确 compose 所有控件（Header + RichLog + StatusPanel + TextArea）
- [ ] 纯黑背景 `#000000`，RichLog 可鼠标选中复制（无需 Shift）
- [ ] Shift+Enter 换行，Enter 提交
- [ ] Ctrl+P 打开命令面板，模糊搜索正常，Enter 执行
- [ ] `/plan` 进入 Plan Mode，StatusPanel 显示 "⚡ Plan Mode"
- [ ] `/exit_plan` 退出 Plan Mode
- [ ] Agent 调用 AskUserQuestionTool 时，RichLog 渲染 QuestionPanel
- [ ] ↑↓ 切换选项，Tab 切换问题，Enter 确认提交
- [ ] 每个问题最后一个选项为 "Other"，选中后可输入自定义文本
- [ ] Header 显示模型名 + 快捷键提示
- [ ] 所有快捷键映射正常工作
- [ ] Phase 1-2 功能全部无回归

---

## 风险

| 风险                             | 概率 | 影响 | 缓解                                                                                        |
| -------------------------------- | ---- | ---- | ------------------------------------------------------------------------------------------- |
| Screen 架构下 widget query 失败  | 中   | 高   | 所有 query 加 try/except；Screen mount 后才执行 agent                                       |
| TextArea 替代 Input 导致兼容问题 | 中   | 中   | 保留 `on_input_submitted` 签名兼容性，TextArea 通过 `on_text_area_key_pressed` 触发提交 |
| Plan Mode 与现有 Agent 循环冲突  | 低   | 高   | Plan Mode 为 UI 层状态；Agent 内部 `_plan_mode` 控制 EnterPlanModeTool 是否可用           |
| 纯黑背景在深色 terminal 中看不清 | 低   | 低   | 使用 `#000000` 纯黑配合 `#e6edf3` 亮色文本，对比度正常                                  |
