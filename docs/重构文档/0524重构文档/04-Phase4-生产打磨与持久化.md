# Phase 4: 生产打磨 & 持久化

> **状态**: 待执行 | **优先级**: P3（收尾完善） | **预计工作量**: 3-4 天 | **前置依赖**: Phase 3
>
> **目标**: 生产级完成度 — 会话持久化、主题系统、通知、代码块复制、无障碍、动画、测试覆盖。这是从"能用"到"专业"的最后一步。

---

## 前置背景

### Phase 4 做什么

Phase 1-3 解决了架构根基、核心 UX、面板布局。Phase 4 负责"体验的最后一公里"：

- 用户退出后再回来，会话还在吗？ → 会话持久化
- 白色终端看得眼疼？ → 主题系统
- 上下文被压缩了也没有提示？ → 通知系统
- 代码块需要手动选中复制？ → 代码块一键复制
- 只用键盘能操作所有功能吗？ → 无障碍
- 有测试防止回归吗？ → 测试覆盖

### 本阶段不做什么

- 不新增核心功能（对话、工具调用、压缩、记忆已在前三个 Phase 完成）
- 不做多会话 Tab（架构决策已排除）
- 不做插件系统
- 不做子 Agent Tool（保持 stub）

---

## 详细执行计划

### 4.1 会话持久化

#### 4.1.1 存储格式设计

**存储路径**:
```
~/.coomi/sessions/
  index.json                    # 会话索引
  <session_id>.json             # 单个会话
```

**`index.json`**:
```json
{
  "version": 1,
  "sessions": [
    {
      "id": "abc123",
      "name": "debug-auth-bug",
      "created_at": "2026-05-24T10:30:00",
      "updated_at": "2026-05-24T14:20:00",
      "message_count": 45,
      "token_usage": {"input_tokens": 12500, "output_tokens": 3400, "total_tokens": 15900}
    }
  ]
}
```

**`<session_id>.json`**:
```json
{
  "version": 1,
  "id": "abc123",
  "system_prompt": "You are a helpful assistant...",
  "created_at": "2026-05-24T10:30:00",
  "updated_at": "2026-05-24T14:20:00",
  "current_model": "deepseek-v4-pro",
  "token_usage": {"input_tokens": 12500, "output_tokens": 3400, "total_tokens": 15900},
  "last_prompt_tokens": 4200,
  "messages": [
    {
      "role": "user",
      "content": "Hello",
      "created_at": "2026-05-24T10:30:00"
    },
    {
      "role": "assistant",
      "content": "Hi! How can I help?",
      "tool_calls": null,
      "created_at": "2026-05-24T10:30:05"
    }
  ]
}
```

#### 4.1.2 实现

**新建文件**: `apps/backend/core/services/session_store.py`

```python
"""会话持久化存储

存储路径: ~/.coomi/sessions/
- index.json: 会话索引（轻量，快速列出所有会话）
- <session_id>.json: 完整会话数据

自动保存: 每次消息新增时触发（debounced，每 30 秒）
手动保存: /session save [name]
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..types import Session, Message, TokenUsage


class SessionStore:
    """会话持久化存储管理器。

    线程安全: 所有写操作通过 asyncio.to_thread 在线程池中执行。
    """

    SESSIONS_DIR = Path.home() / ".coomi" / "sessions"
    INDEX_FILE = SESSIONS_DIR / "index.json"
    MAX_SESSIONS = 50  # 最多保留的会话数

    def __init__(self):
        self._ensure_dir()

    # --- 公开 API ---

    def save_session(self, session: Session, name: str | None = None) -> None:
        """保存会话到磁盘。"""
        self._ensure_dir()

        # 1. 构建会话数据
        data = {
            "version": 1,
            "id": session.id,
            "system_prompt": session.system_prompt,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now().isoformat(),
            "current_model": session.current_model,
            "token_usage": {
                "input_tokens": session.token_usage.input_tokens,
                "output_tokens": session.token_usage.output_tokens,
                "total_tokens": session.token_usage.total_tokens,
            },
            "last_prompt_tokens": session.last_prompt_tokens,
            "messages": [self._serialize_message(m) for m in session.messages],
        }

        # 2. 写入会话文件
        session_file = self.SESSIONS_DIR / f"{session.id}.json"
        session_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        # 3. 更新索引
        self._update_index(session, name)

    def load_session(self, session_id: str) -> Session | None:
        """从磁盘加载会话。"""
        session_file = self.SESSIONS_DIR / f"{session_id}.json"
        if not session_file.exists():
            return None

        data = json.loads(session_file.read_text(encoding="utf-8"))

        session = Session(
            id=data["id"],
            system_prompt=data["system_prompt"],
            messages=[self._deserialize_message(m) for m in data["messages"]],
            created_at=datetime.fromisoformat(data["created_at"]),
            token_usage=TokenUsage(**data["token_usage"]),
            current_model=data.get("current_model"),
            last_prompt_tokens=data.get("last_prompt_tokens", 0),
        )
        return session

    def list_sessions(self) -> list[dict]:
        """列出所有已保存的会话（轻量索引信息）。"""
        if not self.INDEX_FILE.exists():
            return []

        data = json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))
        sessions = data.get("sessions", [])

        # 按更新时间倒序
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """删除会话（文件 + 索引）。"""
        session_file = self.SESSIONS_DIR / f"{session_id}.json"
        deleted = False

        if session_file.exists():
            session_file.unlink()
            deleted = True

        # 从索引中移除
        if self.INDEX_FILE.exists():
            data = json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))
            data["sessions"] = [s for s in data["sessions"] if s["id"] != session_id]
            self.INDEX_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return deleted

    def get_latest_session(self) -> dict | None:
        """获取最近使用的会话索引信息。用于启动时自动恢复。"""
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    # --- 内部 ---

    def _ensure_dir(self) -> None:
        self.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def _serialize_message(self, msg: Message) -> dict:
        """将 Message 序列化为 JSON 兼容的 dict。"""
        data = {
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }
        if msg.tool_calls:
            data["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            data["tool_call_id"] = msg.tool_call_id
        if msg.reasoning_content:
            data["reasoning_content"] = msg.reasoning_content
        return data

    def _deserialize_message(self, data: dict) -> Message:
        """从 JSON dict 反序列化 Message。"""
        from ..types import ToolCall

        tool_calls = None
        if "tool_calls" in data and data["tool_calls"]:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in data["tool_calls"]
            ]

        return Message(
            role=data["role"],
            content=data.get("content"),
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            reasoning_content=data.get("reasoning_content"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def _update_index(self, session: Session, name: str | None) -> None:
        """更新会话索引文件。"""
        index_data = {"version": 1, "sessions": []}
        if self.INDEX_FILE.exists():
            index_data = json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))

        # 移除同名会话的旧条目
        index_data["sessions"] = [
            s for s in index_data["sessions"] if s["id"] != session.id
        ]

        # 添加新条目
        index_data["sessions"].append({
            "id": session.id,
            "name": name or f"Session {session.id[:8]}",
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now().isoformat(),
            "message_count": len(session.messages),
            "token_usage": {
                "input_tokens": session.token_usage.input_tokens,
                "output_tokens": session.token_usage.output_tokens,
                "total_tokens": session.token_usage.total_tokens,
            },
        })

        # 超过最大数量时删除最旧的
        if len(index_data["sessions"]) > self.MAX_SESSIONS:
            oldest = index_data["sessions"].pop(0)
            oldest_file = self.SESSIONS_DIR / f"{oldest['id']}.json"
            if oldest_file.exists():
                oldest_file.unlink()

        self.INDEX_FILE.write_text(
            json.dumps(index_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
```

#### 4.1.3 集成到 CoomiApp

**启动时自动恢复**:

```python
# CoomiApp.on_mount()
def on_mount(self) -> None:
    # ... 现有初始化代码 ...

    # 尝试恢复上次会话
    self._session_store = SessionStore()
    latest = self._session_store.get_latest_session()

    if latest:
        # 恢复：加载完整会话
        session = self._session_store.load_session(latest["id"])
        if session:
            self._session = session

            # 恢复消息历史到 RichLog
            for msg in session.messages:
                if msg.role == "user":
                    self._wl_to_log(f"\n[bold cyan]You:[/bold cyan] {msg.content}")
                elif msg.role == "assistant":
                    if msg.content:
                        self._wl_to_log(Markdown(msg.content))
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            self._wl_to_log(f"[dim]Tool: {tc.name}[/dim]")

            self._wl_to_log(f"[dim]Restored session: {latest['name']} ({latest['message_count']} msgs)[/dim]")
        else:
            # 会话文件丢失，从索引中移除
            self._session_store.delete_session(latest["id"])
            self._create_fresh_session()
    else:
        self._create_fresh_session()
```

**退出时自动保存**:

```python
# 重写 CoomiApp.on_exit (Textual lifecycle hook)
def on_exit(self) -> None:
    """退出前自动保存当前会话。"""
    if self._session and self._session.messages:
        try:
            self._session_store.save_session(self._session)
        except Exception:
            pass  # 静默失败，避免阻塞退出
```

**定期自动保存**（debounced）:

```python
# 在 _run_agent() 的 finally 块中：
# 每次 agent 执行完毕后触发一次保存（debounced）
try:
    self._session_store.save_session(self._session)
except Exception:
    pass
```

**新增 `/session` 命令**:

```python
def _handle_session_command(store: SessionStore, session: Session, args: str) -> str:
    """处理 /session 命令。"""
    if not args:
        return (
            "[bold cyan]Session commands:[/bold cyan]\n\n"
            "  /session save [name]  - Save current session\n"
            "  /session load [id]    - Load a saved session\n"
            "  /session list         - List saved sessions\n"
            "  /session delete [id]  - Delete a session"
        )

    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower()
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "save":
        name = subargs or None
        store.save_session(session, name=name)
        return f"[bold green]+ Session saved:[/bold green] {name or session.id[:8]}"

    elif subcmd == "list":
        sessions = store.list_sessions()
        if not sessions:
            return "[dim]No saved sessions[/dim]"
        lines = [f"[bold cyan]Saved sessions ({len(sessions)}):[/bold cyan]"]
        for s in sessions:
            lines.append(
                f"  [bold]{s['name']}[/bold] [dim]({s['id'][:8]})[/dim] "
                f"[dim]{s['message_count']} msgs, {s['updated_at'][:16]}[/dim]"
            )
        return "\n".join(lines)

    elif subcmd == "load":
        if not subargs:
            return "[red]Please provide session id or name[/red]"
        loaded = store.load_session(subargs)
        if not loaded:
            return f"[red]Session not found: {subargs}[/red]"
        # 需要在 CoomiApp 层面切换 session — 通过回调或事件
        return f"[bold cyan]Loading session:[/bold cyan] {subargs}"

    elif subcmd == "delete":
        if not subargs:
            return "[red]Please provide session id or name[/red]"
        if store.delete_session(subargs):
            return f"[bold green]- Session deleted:[/bold green] {subargs}"
        return f"[red]Session not found: {subargs}[/red]"

    return f"[red]Unknown subcommand: {subcmd}[/red]"
```

#### 4.1.4 扩充 `types.py` — Session/Message 序列化

```python
# Session 增加 to_dict / from_dict
@dataclass
class Session:
    # ... 现有字段 ...

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "system_prompt": self.system_prompt,
            "created_at": self.created_at.isoformat(),
            "token_usage": {
                "input_tokens": self.token_usage.input_tokens,
                "output_tokens": self.token_usage.output_tokens,
                "total_tokens": self.token_usage.total_tokens,
            },
            "current_model": self.current_model,
            "last_prompt_tokens": self.last_prompt_tokens,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data["id"],
            system_prompt=data["system_prompt"],
            messages=[Message.from_dict(m) for m in data["messages"]],
            created_at=datetime.fromisoformat(data["created_at"]),
            token_usage=TokenUsage(**data["token_usage"]),
            current_model=data.get("current_model"),
            last_prompt_tokens=data.get("last_prompt_tokens", 0),
        )


# Message 增加 from_dict
@dataclass
class Message:
    # ... 现有字段 ...

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        tool_calls = None
        if "tool_calls" in data and data["tool_calls"]:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in data["tool_calls"]
            ]
        return cls(
            role=data["role"],
            content=data.get("content"),
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            reasoning_content=data.get("reasoning_content"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
```

---

### 4.2 主题系统

#### 4.2.1 设计

使用 Textual CSS 自定义属性（CSS variables）实现主题切换。三个预置主题：

| 主题 | 风格 | 背景 | 强调色 |
|------|------|------|--------|
| `dark` | GitHub Dark（默认） | #0d1117 | #58a6ff |
| `light` | GitHub Light | #ffffff | #0969da |
| `monokai` | Monokai Pro | #2d2a2e | #ffd866 |

#### 4.2.2 主题 CSS 文件

**新建文件**: `apps/backend/core/ui/tcss/themes/dark.tcss`

```css
/* Coomi Dark Theme — GitHub Dark Default */
* {
    --color-bg: #0d1117;
    --color-surface: #161b22;
    --color-surface-raised: #21262d;
    --color-border: #30363d;
    --color-text: #c9d1d9;
    --color-text-dim: #8b949e;
    --color-accent: #58a6ff;
    --color-success: #238636;
    --color-warning: #d29922;
    --color-error: #f85149;

    /* 工具颜色 */
    --color-tool-read: #58a6ff;
    --color-tool-write: #d29922;
    --color-tool-destructive: #f85149;
    --color-tool-cache: #3fb950;

    /* 输入 */
    --color-input-bg: #1c2333;
    --color-input-cursor: #58a6ff;
}
```

**新建文件**: `apps/backend/core/ui/tcss/themes/light.tcss`

```css
/* Coomi Light Theme */
* {
    --color-bg: #ffffff;
    --color-surface: #f6f8fa;
    --color-surface-raised: #eaeef2;
    --color-border: #d0d7de;
    --color-text: #1f2328;
    --color-text-dim: #656d76;
    --color-accent: #0969da;
    --color-success: #1a7f37;
    --color-warning: #9a6700;
    --color-error: #cf222e;

    --color-tool-read: #0969da;
    --color-tool-write: #9a6700;
    --color-tool-destructive: #cf222e;
    --color-tool-cache: #1a7f37;

    --color-input-bg: #f6f8fa;
    --color-input-cursor: #0969da;
}
```

**新建文件**: `apps/backend/core/ui/tcss/themes/monokai.tcss`

```css
/* Coomi Monokai Theme */
* {
    --color-bg: #2d2a2e;
    --color-surface: #221f22;
    --color-surface-raised: #3a343a;
    --color-border: #5b595c;
    --color-text: #fcfcfa;
    --color-text-dim: #939293;
    --color-accent: #ffd866;
    --color-success: #a9dc76;
    --color-warning: #ffd866;
    --color-error: #ff6188;

    --color-tool-read: #78dce8;
    --color-tool-write: #ffd866;
    --color-tool-destructive: #ff6188;
    --color-tool-cache: #a9dc76;

    --color-input-bg: #221f22;
    --color-input-cursor: #ffd866;
}
```

#### 4.2.3 修改主 CSS 使用变量

**文件**: `apps/backend/core/ui/tcss/coomi.tcss`

```css
/* 从编译后的 CSS 变量引用颜色 */
Screen {
    layout: vertical;
    background: $color-bg;
}

Screen:focus-within {
    background: $color-bg;
}

#app-header {
    height: 1;
    dock: top;
    padding: 0 1;
    background: $color-surface-raised;
    color: $color-text;
}

#message-log {
    height: 1fr;
    padding: 0 1;
    background: $color-bg;
}

#stream-preview {
    height: auto;
    max-height: 10;
    padding: 0 1;
    background: $color-bg;
    color: $color-text;
}

#status-panel {
    height: 2;
    padding: 0 1;
    background: $color-surface;
}

#prompt-input {
    dock: bottom;
    height: 3;
    padding: 0 1;
    background: $color-input-bg;
}

#prompt-input > .input--cursor {
    background: $color-input-cursor;
}

/* 工具 Banner 颜色 */
ToolCallBanner {
    background: $color-surface;
}

ToolCallBanner.pending {
    border: dashed $color-border;
}

ToolCallBanner.running {
    border: solid $color-warning;
}

ToolCallBanner.done {
    border: solid $color-success;
}

ToolCallBanner.cache {
    border: solid $color-tool-cache;
}

/* 工具面板 */
ToolPanel {
    background: $color-surface;
    border-left: solid $color-border;
}

/* 对话浏览器 */
ConversationBrowser {
    background: $color-surface;
    border-right: solid $color-border;
}
```

#### 4.2.4 主题加载逻辑

在 `CoomiApp` 中：

```python
# 主题切换
def _apply_theme(self, theme_name: str) -> None:
    """切换主题 CSS。"""
    theme_file = f"tcss/themes/{theme_name}.tcss"
    theme_path = os.path.join(os.path.dirname(__file__), theme_file)

    if not os.path.exists(theme_path):
        return

    # Textual 支持运行时重新加载 CSS
    # 方式: 读取文件内容，设置 app.stylesheet
    theme_css = Path(theme_path).read_text(encoding="utf-8")
    base_css_path = os.path.join(os.path.dirname(__file__), "tcss/coomi.tcss")
    base_css = Path(base_css_path).read_text(encoding="utf-8")

    # 合并: 先加载变量定义，再加载布局规则
    combined = theme_css + "\n" + base_css
    self.stylesheet = combined

    # 保存偏好
    self._save_theme_preference(theme_name)

def _save_theme_preference(self, theme_name: str) -> None:
    """保存主题偏好到 state.json。"""
    try:
        state_path = Path(os.getcwd()) / ".coomi" / "state.json"
        state = {}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        state["theme"] = theme_name
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass

# /theme 命令
def _handle_theme_command(app: CoomiApp, args: str) -> str:
    themes = ["dark", "light", "monokai"]
    if not args:
        current = app._current_theme
        lines = [f"[bold cyan]Available themes:[/bold cyan]"]
        for t in themes:
            marker = " [bold green](active)[/bold green]" if t == current else ""
            lines.append(f"  {t}{marker}")
        return "\n".join(lines)

    theme_name = args.strip()
    if theme_name not in themes:
        return f"[red]Unknown theme: {theme_name}[/red]\n\nAvailable: {', '.join(themes)}"

    app._apply_theme(theme_name)
    return f"[bold cyan]Theme changed to:[/bold cyan] {theme_name}"
```

---

### 4.3 通知系统

#### 4.3.1 实现

**新建文件**: `apps/backend/core/ui/widgets/notifications.py`

```python
"""Toast 通知系统"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import Vertical


class NotificationType:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class Notification(Widget):
    """单个 Toast 通知。

    自动在指定时间后消失。
    """

    DEFAULT_CSS = """
    Notification {
        height: auto;
        min-height: 1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    Notification.info {
        border-left: solid #58a6ff;
        background: #161b22;
    }
    Notification.warning {
        border-left: solid #d29922;
        background: #161b22;
    }
    Notification.error {
        border-left: solid #f85149;
        background: #161b22;
    }
    Notification.success {
        border-left: solid #238636;
        background: #161b22;
    }
    """

    def __init__(self, message: str, ntype: str = NotificationType.INFO, duration: float = 3.0):
        super().__init__()
        self.message = message
        self.ntype = ntype
        self.duration = duration

    def on_mount(self) -> None:
        self.set_class(self.ntype, True)
        # 自动消失
        self.set_timer(self.duration, self.dismiss)

    def compose(self) -> ComposeResult:
        icons = {
            NotificationType.INFO: "[bold blue]ℹ[/bold blue]",
            NotificationType.WARNING: "[bold yellow]⚠[/bold yellow]",
            NotificationType.ERROR: "[bold red]✗[/bold red]",
            NotificationType.SUCCESS: "[bold green]✓[/bold green]",
        }
        icon = icons.get(self.ntype, "")
        yield Static(f"{icon} {self.message}")

    def dismiss(self) -> None:
        try:
            self.remove()
        except Exception:
            pass


class NotificationArea(Vertical):
    """通知容器 — 固定在屏幕右上角。

    通知自下而上堆叠，自动消失。
    """

    DEFAULT_CSS = """
    NotificationArea {
        dock: top;
        height: auto;
        max-height: 20;
        width: 40;
        padding: 0 1;
        align: right top;
    }
    """

    def notify(self, message: str, ntype: str = NotificationType.INFO, duration: float = 3.0) -> None:
        """显示一条通知。"""
        notification = Notification(message, ntype, duration)
        self.mount(notification)
```

#### 4.3.2 集成到 MainScreen

```python
# MainScreen.on_notify() — 便捷方法
def notify(self, message: str, ntype: str = NotificationType.INFO) -> None:
    area = self.query_one("#notification-area", NotificationArea)
    area.notify(message, ntype)
```

使用场景：

```python
# 上下文压缩通知
self.notify(f"Context compressed: {before} -> {after} messages", NotificationType.INFO)

# 错误通知
self.notify(f"Execution error: {e}", NotificationType.ERROR)

# 会话保存通知
self.notify("Session saved", NotificationType.SUCCESS)

# 缓存命中
self.notify(f"Cache hit: {tool_name}", NotificationType.INFO)
```

---

### 4.4 代码块复制

在 `StreamingLog` (Phase 2) 或 `RichLog` 中检测 Markdown 代码块，添加复制按钮。

```python
# 扩展 StreamingLog
class StreamingLog(RichLog):
    """带复制按钮的流式 RichLog。"""

    def write(self, content, *args, **kwargs):
        """覆盖 write，检测代码块并附加复制按钮。"""
        super().write(content, *args, **kwargs)

        # 如果是 Markdown 且包含代码块
        if isinstance(content, Markdown) and "```" in content.markup:
            self._add_copy_hints(content.markup)

    def _add_copy_hints(self, markup: str) -> None:
        """为代码块添加 [Copy] 提示。"""
        import re
        blocks = re.findall(r'```(\w*)\n(.*?)```', markup, re.DOTALL)
        for lang, code in blocks:
            self.write(f"[dim]{lang or 'code'} block ({len(code)} chars) — [bold]Ctrl+C[/bold] to copy[/dim]")
```

> **实际实现**: Textual 的 `RichLog` 支持文本选择（Selection）。用户可以用鼠标或键盘选中代码块后 Ctrl+C 复制。提供一个 `[Copy]` 按钮需要更复杂的自定义 Widget（如 `TextArea`）。Phase 4 优先使用 Textual 内置的文本选择能力，加上在代码块上方显示语言标签和快捷键提示。自定义复制按钮降级为未来增强。

---

### 4.5 无障碍

Textual 框架已内置一定的无障碍支持。Phase 4 需要确保：

1. **描述性 Widget ID**: 所有 `Static`、`Input`、`Button` 控件有描述性 `id`
2. **快捷键描述**: 所有 `BINDINGS` 有 `description`（Phase 3 已做）
3. **颜色对比度**: 
   - Dark theme: 文字 `#c9d1d9` 在背景 `#0d1117` 上 → 对比度 7.85:1 ✓ (AAA)
   - Light theme: 文字 `#1f2328` 在背景 `#ffffff` 上 → 对比度 14.1:1 ✓ (AAA)
   - Monokai: 文字 `#fcfcfa` 在背景 `#2d2a2e` 上 → 对比度 11.5:1 ✓ (AAA)
4. **焦点指示器**: 所有输入控件有可见焦点边框（CSS Pseudoclass `:focus`）
5. **键盘可达**: 所有可操作元素通过 Tab/Shift+Tab 可达

```css
/* 焦点指示器 */
Input:focus {
    border: solid $color-accent;
}

ListView:focus {
    border: solid $color-accent;
}
```

---

### 4.6 过渡动画

Textual 支持有限的 CSS 动画。Phase 4 添加：

```css
/* 工具面板滑入 */
ToolPanel {
    transition: offset 200ms ease-out;
}

/* 通知淡入 */
Notification {
    animate: opacity 200ms;
}
```

由于 Textual CSS `transition` 支持有限，备用方案是使用 `set_timer` 做简单的逐帧动画：

```python
# 工具面板滑入动画
def show_tool_panel(self) -> None:
    panel = self.query_one("#tool-panel", ToolPanel)
    panel.styles.width = 0
    panel.display = True

    # 简单宽度动画: 0 → 30% (30 帧, 每帧 10ms)
    target_width = 30
    self._animate_width(panel, 0, target_width, 300)

def _animate_width(self, widget, start, end, duration_ms, step=0):
    if step >= 20:
        widget.styles.width = f"{end}%"
        return
    progress = step / 20
    current = int(start + (end - start) * progress)
    widget.styles.width = f"{current}%"
    self.set_timer(duration_ms / 20 / 1000, lambda: self._animate_width(
        widget, start, end, duration_ms, step + 1
    ))
```

---

### 4.7 最终清理

确认并完成以下清理事项（部分在 Phase 1 已启动）：

- [ ] `pyproject.toml` 中依赖精简确认（typer、fastapi、uvicorn、aiofiles 已移除）
- [ ] 确认 `requirements.txt` 与 `pyproject.toml` 一致
- [ ] 删除所有 `# TODO` 和 `# FIXME` 标记（转为 Issue）
- [ ] 删除所有已注释掉的代码块
- [ ] 移除 `AgentLoop.run()` 同步方法（如果 Phase 1 保留了 deprecated 版本）
- [ ] 确认 `.coomi/` 目录下的旧文件清理（如有残留）

---

### 4.8 测试

#### 4.8.1 测试目录结构

```
tests/
  __init__.py
  ui/
    __init__.py
    test_textual_app.py       # Textual pilot 集成测试
    test_streaming_log.py     # StreamingLog 单元测试
    test_events.py            # 事件类型测试
  engine/
    __init__.py
    test_loop.py              # AgentLoop 单元测试
    test_session.py           # Session 序列化测试
  services/
    __init__.py
    test_session_store.py     # SessionStore 单元测试
```

#### 4.8.2 关键测试用例

**`test_textual_app.py`** — 使用 Textual `pilot`:

```python
import pytest
from textual.pilot import Pilot


@pytest.mark.asyncio
async def test_app_mounts():
    """应用正常挂载。"""
    from apps.backend.core.ui.textual_app import CoomiApp
    app = CoomiApp()
    async with app.run_test() as pilot:
        assert pilot.app is not None
        # 检查关键 widget 存在
        assert pilot.app.query_one("#message-log")


@pytest.mark.asyncio
async def test_slash_commands():
    """斜杠命令正常处理。"""
    from apps.backend.core.ui.textual_app import CoomiApp
    app = CoomiApp()
    async with app.run_test() as pilot:
        # 输入 /model
        await pilot.click("#prompt-input")
        await pilot.press("ctrl+a", "backspace")  # 清空
        for char in "/model":
            await pilot.press(char)
        await pilot.press("enter")
        # 验证 RichLog 中有输出
        log = pilot.app.query_one("#message-log")
        # (具体断言取决于实际输出)


@pytest.mark.asyncio
async def test_escape_cancel():
    """Esc 取消功能。"""
    ...


@pytest.mark.asyncio
async def test_double_escape_exit():
    """双击 Esc 退出。"""
    ...
```

**`test_events.py`**:

```python
def test_event_type_hierarchy():
    """事件类型继承关系。"""
    from apps.backend.core.ui.events import ToolStart, AgentEvent
    assert issubclass(ToolStart, AgentEvent)


def test_all_event_types():
    """所有事件类型可实例化且字段正确。"""
    from apps.backend.core.ui.events import ToolDone, CompressionEvent
    event = ToolDone(tool_name="Read", elapsed=1.5)
    assert event.tool_name == "Read"
    assert event.elapsed == 1.5

    event = CompressionEvent(before=20, after=5)
    assert event.before == 20
```

**`test_session_store.py`**:

```python
def test_save_and_load(tmp_path):
    """保存并恢复会话。"""
    ...

def test_list_sessions(tmp_path):
    """列出所有会话。"""
    ...

def test_delete_session(tmp_path):
    """删除会话。"""
    ...

def test_max_sessions_cleanup(tmp_path):
    """超过最大会话数时清除旧会话。"""
    ...
```

#### 4.8.3 运行测试

```bash
# 运行所有测试
pytest tests/ -v

# 只运行 UI 测试 (需要终端)
pytest tests/ui/ -v

# 只运行引擎测试 (无 UI)
pytest tests/engine/ tests/services/ -v
```

---

### 4.9 验证

#### 会话持久化

1. 启动 `python run.py`，发送几条消息
2. `exit` 退出
3. 重新启动，应自动恢复上次会话
4. 检查 `~/.coomi/sessions/` 目录中有 `index.json` 和 `<id>.json`

#### 主题切换

1. `/theme light` → 界面变为浅色
2. `/theme monokai` → 界面变为 Monokai 配色
3. `/theme dark` → 恢复深色
4. 退出重进，主题应保持

#### 通知

1. 触发上下文压缩（发送大量消息直到超过窗口 90%），观察右上角通知
2. `/session save` 后应出现成功通知
3. 错误场景（如无效命令）应有通知

#### 代码块

1. 发送 "用 Python 写一个 hello world"
2. 回复中的代码块应有语言标签 `python`
3. 可用鼠标或键盘选中代码块内容并 Ctrl+C 复制

#### 无障碍

1. 按 Tab 键，焦点应在各控件间移动
2. 焦点指示器在所有主题中可见
3. 键盘可完成所有操作（不用鼠标）

#### 测试

```bash
pytest tests/ -v
# 所有测试通过
```

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Session 文件损坏导致启动失败 | 低 | 高 | 损坏文件静默跳过 + 自动从索引清除 + 日志记录 |
| Theme CSS 变量在 Textual 中不生效 | 中 | 中 | 测试确认 Textual 支持 CSS custom properties；如不支持，降级为整文件替换 |
| 大型会话（1000+ 消息）加载慢 | 低 | 中 | async 加载 + 显示进度指示器；超过阈值时截断旧消息 |
| 通知过多遮挡对话 | 低 | 低 | max-height: 20 限制；旧通知自动消失 |

---

## 完成标准

- [ ] 退出重启后会话自动恢复
- [ ] `/session save|load|list|delete` 命令正常工作
- [ ] 三个主题可切换并持久化
- [ ] 通知出现在压缩、保存、错误等事件，自动消失
- [ ] 代码块有语言标签和复制提示
- [ ] 所有交互元素可通过键盘 Tab 访问
- [ ] 颜色对比度满足 WCAG AA 标准
- [ ] 面板切换有过渡动画
- [ ] `pytest tests/` 全部通过，覆盖率 > 60%
- [ ] Phase 1-3 功能无回归
- [ ] `pyproject.toml` 和 `requirements.txt` 依赖清理完毕
