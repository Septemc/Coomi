# Phase 2: 增量流式渲染 & 实时工具展示

> **状态**: 待执行 | **优先级**: P1（核心 UX 改进） | **预计工作量**: 3-4 天 | **前置依赖**: Phase 1
>
> **目标**: 文本逐字流式呈现 + 工具调用实时通知，最大用户体验提升。让 Agent 从"等一会儿突然蹦出结果"变成"边说边做"。

---

## 前置背景

### 当前问题

1. **文本整段刷新** — 当前 `_stream_buffer += chunk` 累积所有文本，最后 `log.write(Markdown(self._stream_buffer))` 一次性渲染。用户看到的是：先空白几秒，然后整段 Markdown 同时出现。旧的 `stream_renderer.py` 曾做过 50ms 节流的增量渲染，但被删除了。
2. **工具名称延迟显示** — LLM 流中 tool_call chunk 是逐步到达的（先 function.name，再 function.arguments），但 `chat_stream_with_tools` 将所有 tool calls 累积到流结束后才一次性 yield。导致 UI 在工具执行开始前一直显示 "◎ Thinking..."。
3. **推理内容不可见** — DeepSeek 的 `reasoning_content` 在流中到达，但当前只记录到变量，不显示。
4. **工具结果无格式化** — 工具完成时只显示 `tool_name + args + ✓ (elapsed)`，不展示结果内容。

### Phase 2 要交付什么

- **StreamingLog** 控件：增量 Markdown 渲染（50ms 节流，段落级替换）
- **工具提前通知**：LLM 流中 tool name 一出现就通知 UI
- **ToolCallBanner** 控件：工具调用生命周期可视化（PENDING → RUNNING → DONE）
- **推理内容折叠**：thinking mode 可展开/折叠

---

## 详细执行计划

### 2.1 LLM 流中提前通知工具调用

#### 2.1.1 问题分析

当前 `generic.py` 的 `chat_stream_with_tools` 流程：

```
API stream chunk 到达:
  chunk.choices[0].delta.tool_calls[0].function.name = "ReadTool"    ← 最早出现
  chunk.choices[0].delta.tool_calls[0].function.arguments = '{"file'  ← 逐步累积
  chunk.choices[0].delta.tool_calls[0].function.arguments = '_path":'  ← ...
  chunk.choices[0].delta.tool_calls[0].function.arguments = ' "/etc"'  ← ...
  [流结束]
  yield {"type": "tool_call", "data": {...}}                           ← 此时才通知 UI！
```

UI 在 "ReadTool" name 到达时（第 1 个 chunk）就应该能显示 "ReadTool 准备执行..."，而不是等到参数全收完。

#### 2.1.2 修改 `generic.py`

**文件**: `apps/backend/core/services/llm/generic.py`

在 `chat_stream_with_tools` 中添加提前通知逻辑：

```python
async def chat_stream_with_tools(self, messages, tools=None, **kwargs) -> AsyncIterator[dict]:
    params = self._build_params(messages, tools, stream=True)
    response = await self.client.chat.completions.create(**params)

    tool_calls_accum: dict[int, dict] = {}
    tool_names_seen: set[int] = set()  # 新增：跟踪已通知的 tool index
    usage_chunk = None

    async for chunk in response:
        delta = chunk.choices[0].delta

        # --- 新增：提前通知工具调用 ---
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_names_seen and tc_delta.function and tc_delta.function.name:
                    tool_names_seen.add(idx)
                    yield {
                        "type": "tool_call_start",
                        "tool_name": tc_delta.function.name,
                        "index": idx,
                    }

                # 累积 tool call 参数（原有逻辑不变）
                if idx not in tool_calls_accum:
                    tool_calls_accum[idx] = {
                        "id": tc_delta.id or "",
                        "name": "",
                        "arguments": "",
                    }
                tc = tool_calls_accum[idx]
                if tc_delta.id:
                    tc["id"] = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        tc["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        tc["arguments"] += tc_delta.function.arguments

        # 文本内容（原有逻辑不变）
        if delta.content:
            yield {"type": "content", "content": delta.content}

        # 推理内容（原有逻辑不变）
        model_extra = getattr(chunk.choices[0], "model_extra", {}) or {}
        if model_extra.get("reasoning_content"):
            yield {"type": "reasoning_content", "content": model_extra["reasoning_content"]}

        # 用量（原有逻辑不变）
        if hasattr(chunk, "usage") and chunk.usage:
            usage_chunk = {
                "prompt_tokens": chunk.usage.prompt_tokens,
                "completion_tokens": chunk.usage.completion_tokens,
                "total_tokens": chunk.usage.total_tokens,
            }

    # 流结束后 yield 完整 tool calls（原有逻辑不变）
    for idx in sorted(tool_calls_accum.keys()):
        tc = tool_calls_accum[idx]
        tc["arguments"] = json.loads(tc["arguments"]) if tc["arguments"] else {}
        yield {"type": "tool_call", "data": tc}

    if usage_chunk:
        yield {"type": "usage", "data": usage_chunk}
```

#### 2.1.3 修改 `loop.py` — 转发 ToolCallStart

**文件**: `apps/backend/core/engine/loop.py`

```python
# 在 run_stream() 的 async for chunk 循环中，新增事件类型处理：
if chunk["type"] == "tool_call_start":
    # 提前通知 UI：工具即将执行，但参数尚未完整
    yield ToolStart(tool_name=chunk["tool_name"], arguments={})
elif chunk["type"] == "tool_call":
    # 流结束后的完整 tool call — 此时参数完整
    tool_calls_data.append(chunk["data"])
```

注意：`ToolStart` 可能被 yield 两次（一次提前通知参数为空，一次在 tool_call 阶段参数完整）。UI 需要处理这种"更新"场景 — 见 2.3 ToolCallBanner。

#### 2.1.4 修改 `anthropic.py` — 同样提前通知

**文件**: `apps/backend/core/services/llm/anthropic.py`

Anthropic 的流式事件结构不同——`content_block_start` 事件在工具调用开始时就会触发：

```python
async with self.client.messages.stream(...) as stream:
    async for event in stream:
        if event.type == "content_block_start":
            if event.content_block.type == "tool_use":
                # Anthropic 在这里就能拿到 tool name!
                yield {
                    "type": "tool_call_start",
                    "tool_name": event.content_block.name,
                    "index": event.index,
                }
        elif event.type == "content_block_delta":
            if event.delta.type == "input_json_delta":
                # 累积 arguments...
                ...
```

Anthropic 的提前通知比 OpenAI 更自然——`content_block_start` 事件本身就包含了 tool name。

---

### 2.2 StreamingLog 控件（增量 Markdown 渲染）

#### 2.2.1 设计目标

- 文本逐字到达时，用户能看到渐进式输出
- 50ms 节流，避免每收到一个 token 就重绘一次
- 使用"追加新段落 + 替换最后段落"策略，不清空历史消息
- 继承 Textual 的 `RichLog`，兼容 Rich Markdown 渲染

#### 2.2.2 实现

**新建文件**: `apps/backend/core/ui/widgets/streaming_log.py`

```python
"""StreamingLog — 支持增量 Markdown 渲染的 RichLog 子类"""
from __future__ import annotations

from rich.markdown import Markdown
from textual.widgets import RichLog


class StreamingLog(RichLog):
    """RichLog 子类，支持流式文本的增量渲染。

    工作方式:
    1. feed(text) 追加文本到内部 buffer
    2. 50ms 节流 timer 触发 flush
    3. flush 时：清除上次渲染的"尾段"，用当前完整 buffer 重新渲染
    4. finalize() 停止 timer，最终 flush
    """

    THROTTLE_MS = 50  # 渲染节流间隔

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream_buffer: str = ""
        self._render_timer = None
        self._stream_start_line: int = 0  # 流式输出的起始行号（在 RichLog 中）
        self._is_streaming: bool = False
        self._pending_content: str = ""

    def begin_stream(self) -> None:
        """标记流式输出的开始。后续 feed() 调用会增量渲染。"""
        self._stream_buffer = ""
        self._is_streaming = True
        # 记录当前行号，后续 flush 时替换此位置之后的内容
        self._stream_start_line = self.line_count

    def feed(self, text: str) -> None:
        """追加流式文本片段。触发节流渲染。"""
        if not self._is_streaming:
            self.begin_stream()
        self._stream_buffer += text
        self._start_throttle()

    def _start_throttle(self) -> None:
        """启动或重置节流 timer。"""
        if self._render_timer is not None:
            return  # timer 已在运行，等待下次触发
        self._render_timer = self.set_timer(
            self.THROTTLE_MS / 1000, self._flush
        )

    def _flush(self) -> None:
        """将 buffer 刷入 RichLog。这是节流后的实际渲染操作。"""
        self._render_timer = None

        if not self._stream_buffer and self._pending_content == self._stream_buffer:
            return

        # 策略：清除上一次的流式段落，写入当前完整 buffer
        # RichLog 不直接支持"替换最后 N 行"，使用以下方法：
        # 1. 如果是第一次 flush，直接 write
        # 2. 如果已写入过，先清除之前的流式行再重新写
        #
        # 实际实现：RichLog 不支持精确的行删除。
        # 替代方案：用 RichLog.clear() + 重新渲染全部历史（不适合长对话）
        #
        # 最终方案：使用 RichLog 的 write() 追加模式，
        # 每次 flush 追加 "[streaming]当前buffer[/streaming]" 标记段，
        # 下次 flush 时用 ANSI 码退行删除上一个标记段再写入新的。
        #
        # 但 Textual 的 RichLog 是虚拟 terminal，不是真实终端。
        # 最可行的方案：只最终一次性渲染。
        # 降级方案：使用 Static widget 作为流式预览区，最终合并入 RichLog。

        # 决定：采用"分离式预览"策略
        # 此处仅做 buffer 累积，实际在父组件中用一个 Static 做流式预览
        # 详见 textual_app.py 中的集成方式
        self._pending_content = self._stream_buffer

    def finalize(self) -> str:
        """停止流式输出，返回完整文本供写入 RichLog Markdown。"""
        self._is_streaming = False
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None
        content = self._stream_buffer
        self._stream_buffer = ""
        self._pending_content = ""
        return content
```

#### 2.2.3 实际渲染策略 — 分离式预览

> **重要设计决策**: Textual 的 `RichLog` 不支持原地替换已有的文本行。有两种方案：
>
> **A) 分离式预览（推荐）** — 流式文本渲染在一个独立的 `Static` 控件中，最终结果合并入 `RichLog`。用户看到："流式预览区逐字出现 → 完成后移入消息历史"。
>
> **B) 全量重渲染** — 每次 flush 用 `RichLog.clear()` 清空后重写全部消息 + 当前 buffer。简单但 O(n) 成本，长对话会明显卡顿。
>
> **选择**: Phase 2 采用方案 A（分离式预览）。Phase 3 如果要实现更好的原地流式效果，可以升级为自定义 Widget。

**修改 `textual_app.py`**:

在 `compose()` 中，将 `Static(#stream-indicator)` 复用于流式文本预览：

```python
# compose() 保持现有布局
def compose(self) -> ComposeResult:
    yield RichLog(id="message-log", markup=True, wrap=True, highlight=True)
    yield StreamingPreview(id="stream-preview")  # 改名，既做 indicator 又做流式预览
    yield StatusPanel(self.status_line, id="status-panel")
    yield Input(id="prompt-input", placeholder="> ...")
```

**新建 `StreamingPreview`**:

```python
class StreamingPreview(Static):
    """流式文本预览控件。

    在 Agent 思考/执行期间显示：
    - "◎ Thinking..." (初始状态)
    - "⟳ ToolName..." (工具执行中)
    - 流式 Markdown 预览 (文本到达时)
    """

    def show_text(self, text: str) -> None:
        """显示流式文本预览（截取最后 500 字符）。"""
        preview = text[-500:] if len(text) > 500 else text
        self.update(Markdown(preview))

    def show_thinking(self) -> None:
        self.update("[bold yellow]◎ Thinking...[/bold yellow]")

    def show_tool(self, tool_name: str) -> None:
        self.update(f"[bold yellow]⟳ {tool_name}...[/bold yellow]")

    def clear_preview(self) -> None:
        self.update("")
```

在 `_run_agent()` 的流式循环中：

```python
# 收到文本 chunk 时
if isinstance(chunk, str):
    self._stream_buffer += chunk
    preview.show_text(self._stream_buffer)  # 增量预览

# 工具开始
elif isinstance(chunk, ToolStart):
    preview.show_tool(chunk.tool_name)

# 流式迭代结束后
if self._stream_buffer.strip():
    log.write(Markdown(self._stream_buffer))  # 最终写入 RichLog
preview.clear_preview()
```

#### 2.2.4 CSS 样式

```css
#stream-preview {
    height: auto;
    max-height: 8;
    padding: 0 1;
    background: #0d1117;
    color: #c9d1d9;
    overflow-y: auto;
}
```

---

### 2.3 ToolCallBanner 控件

#### 2.3.1 设计

```
┌────────────────────────────────────────────────┐
│ 🔧 ReadTool  /path/to/file.py (lines 10-200)   │ ← PENDING: 工具名已知，参数待补全
│    ⠋ Executing...                               │ ← RUNNING: spinner + 计时
│    ✓ Done (1.2s)  [cache]                      │ ← DONE: 耗时 + 缓存标记
│    ┌──────────────────────────────────────┐     │
│    │ Result (3421 chars) [+]             │     │ ← 展开/折叠的结果预览
│    └──────────────────────────────────────┘     │
└────────────────────────────────────────────────┘
```

#### 2.3.2 实现

**新建文件**: `apps/backend/core/ui/widgets/tool_call_banner.py`

```python
"""ToolCallBanner — 工具调用生命周期可视化控件"""
from __future__ import annotations

import time
from rich.table import Table
from rich.panel import Panel
from textual.widget import Widget

from ..tool_formatter import format_tool_display
from ...tools.base import ToolAccess


class ToolCallBanner(Widget):
    """内联工具调用 banner。

    生命周期: PENDING → RUNNING → DONE
    """

    DEFAULT_CSS = """
    ToolCallBanner {
        height: auto;
        min-height: 1;
        padding: 0 1;
        margin: 1 0;
    }
    ToolCallBanner.pending {
        border: dashed #30363d;
    }
    ToolCallBanner.running {
        border: solid #d29922;
    }
    ToolCallBanner.done {
        border: solid #238636;
    }
    ToolCallBanner.cache {
        border: solid #58a6ff;
    }
    """

    RESULT_PREVIEW_LENGTH = 300

    def __init__(self, tool_name: str, access: ToolAccess = ToolAccess.READ):
        super().__init__()
        self.tool_name = tool_name
        self.access = access
        self._state = "pending"  # pending | running | done
        self._arguments: dict = {}
        self._start_time: float = time.time()
        self._elapsed: float = 0
        self._result_text: str = ""
        self._cache_hit: bool = False
        self._expanded: bool = False

    # --- 公开 API ---

    def set_arguments(self, arguments: dict) -> None:
        self._arguments = arguments
        self.refresh()

    def set_running(self) -> None:
        self._state = "running"
        self._start_time = time.time()
        self.set_class(self._state, True)
        self.refresh()

    def set_done(self, result_text: str = "", cache_hit: bool = False) -> None:
        self._state = "done"
        self._elapsed = time.time() - self._start_time
        self._result_text = result_text
        self._cache_hit = cache_hit
        self.set_class(self._state, True)
        if cache_hit:
            self.set_class("cache", True)
        self.refresh()

    def toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self.refresh()

    # --- 渲染 ---

    def render(self) -> Table:
        table = Table.grid(padding=(0, 0))

        # 第一行：图标 + 工具名 + 参数预览
        icon = self._get_icon()
        display = format_tool_display(self.tool_name, self._arguments)
        status = self._get_status_text()
        table.add_row(f"{icon} [bold]{self.tool_name}[/bold] {display} {status}")

        # 第二行：结果预览（仅在 DONE 状态且有结果时显示）
        if self._state == "done" and self._result_text:
            preview = self._get_result_preview()
            table.add_row(preview)

        return table

    def _get_icon(self) -> str:
        if self._cache_hit:
            return "[bold green]✓[/bold green]"
        if self._state == "done":
            return "[bold green]✓[/bold green]"
        if self._state == "running":
            return "[bold yellow]⟳[/bold yellow]"
        return "[dim]○[/dim]"

    def _get_status_text(self) -> str:
        if self._state == "pending":
            return "[dim]waiting...[/dim]"
        if self._state == "running":
            return f"[bold yellow]Executing... ({self._elapsed:.1f}s)[/bold yellow]"
        if self._cache_hit:
            return f"[dim]✓ cache ({self._elapsed:.1f}s)[/dim]"
        return f"[dim]✓ ({self._elapsed:.1f}s)[/dim]"

    def _get_result_preview(self) -> str:
        if not self._result_text:
            return ""
        toggle = "[-]" if self._expanded else "[+]"
        if self._expanded:
            return f"[dim]{self._result_text[:1000]}[/dim]"
        preview = self._result_text[:self.RESULT_PREVIEW_LENGTH]
        if len(self._result_text) > self.RESULT_PREVIEW_LENGTH:
            preview += "..."
        return f"[dim]{preview}[/dim] [bold]{toggle} Expand[/bold]"

    def _get_access_color(self) -> str:
        colors = {
            ToolAccess.READ: "#58a6ff",       # 蓝 — 只读
            ToolAccess.WRITE: "#d29922",      # 黄 — 写入
            ToolAccess.DESTRUCTIVE: "#f85149", # 红 — 破坏性
        }
        return colors.get(self.access, "#c9d1d9")
```

#### 2.3.3 集成到 textual_app.py

在 `_run_agent()` 的流式事件处理中：

```python
# ToolStart — 创建 banner
elif isinstance(chunk, ToolStart):
    banner = ToolCallBanner(
        tool_name=chunk.tool_name,
        access=self._tool_registry.get_access(chunk.tool_name),
    )
    if chunk.arguments:
        banner.set_arguments(chunk.arguments)
    # 将 banner 写入 RichLog（作为内联元素）
    self._current_banner = banner
    self._tool_start_time = time.time()
    preview.show_tool(chunk.tool_name)

# 后续收到完整 tool_call 数据时 — 更新 banner
# （在 tool_calls_data 处理循环中）
for tc in tool_calls_data:
    if self._current_banner:
        self._current_banner.set_arguments(tc["arguments"])

# ToolRunning — 更新 banner 状态
elif isinstance(chunk, ToolRunning):
    if self._current_banner:
        self._current_banner.set_running()

# ToolDone — banner 完成
elif isinstance(chunk, ToolDone):
    if self._current_banner:
        self._current_banner.set_done(
            result_text=result_text,  # 需要从 AgentLoop 获取
            cache_hint=False,
        )
    # 将完成后的 banner 写入消息日志
    log.write(self._current_banner)
    self._current_banner = None

# ToolCacheHit — banner 完成（缓存命中）
elif isinstance(chunk, ToolCacheHit):
    if self._current_banner:
        self._current_banner.set_done(cache_hit=True)
    log.write(self._current_banner)
    self._current_banner = None
```

> **注意**: 当前 AgentLoop 的 `ToolDone` 事件只包含 `tool_name` 和 `elapsed`，不包含 `result_text`。需要在 Phase 2 中扩展 `ToolDone` 事件，增加可选的 `result_preview: str | None` 字段。或者让 UI 通过 `ToolRegistry` 自行获取结果。

> **简化方案（Phase 2 采用）**: `ToolDone` 增加 `result_preview: str | None = None` 字段。AgentLoop 在 `_execute_tool_async` 后，将 `result_text[:300]` 传入 `ToolDone`。

---

### 2.4 推理内容展示（DeepSeek thinking mode）

#### 2.4.1 设计

```
┌──────────────────────────────────────┐
│ [Thinking (2.3s)] ▼                  │  ← 可折叠标题
│ ┌──────────────────────────────────┐ │
│ │ 我需要理解用户的请求...           │ │  ← 展开的推理内容（dimmed）
│ │ 首先读取文件，然后分析内容...     │ │
│ └──────────────────────────────────┘ │
│ [Agent 的实际回复...]                │  ← 回复内容（正常显示）
└──────────────────────────────────────┘
```

#### 2.4.2 实现

在 `textual_app.py` 的 `_run_agent()` 中：

```python
full_reasoning = ""
reasoning_start_time = 0
reasoning_visible = True  # Ctrl+R 切换

# 在流式循环中
async for chunk in self._agent.run_stream(self._session, user_input):
    if isinstance(chunk, ReasoningChunk):
        if not full_reasoning:
            reasoning_start_time = time.time()
        full_reasoning += chunk.content

    elif isinstance(chunk, str):
        # 第一个文本 chunk 到达时，如有推理内容则先渲染
        if full_reasoning and reasoning_visible:
            elapsed = time.time() - reasoning_start_time
            reasoning_block = (
                f"[dim]┌─ [Thinking ({elapsed:.1f}s)] ▼[/dim]\n"
                f"[dim]│ {full_reasoning.replace(chr(10), chr(10) + '│ ')}[/dim]\n"
                f"[dim]└─[/dim]"
            )
            log.write(Markdown(reasoning_block))
            full_reasoning = ""  # 已渲染，清空

        # 正常文本流式输出
        self._stream_buffer += chunk
        preview.show_text(self._stream_buffer)
```

#### 2.4.3 Ctrl+R 切换推理可见性

在 `CoomiApp` 中添加 binding：

```python
BINDINGS = [
    ("escape", "cancel_or_exit", "Cancel / Exit"),
    ("ctrl+r", "toggle_reasoning", "Toggle reasoning"),
]
```

```python
def action_toggle_reasoning(self) -> None:
    self._reasoning_visible = not self._reasoning_visible
    # 如果正在流式输出中，重新渲染当前内容
```

---

### 2.5 更新 CSS

**文件**: `apps/backend/core/ui/tcss/coomi.tcss`

```css
/* ===== Phase 2 新增样式 ===== */

/* 流式预览区 */
#stream-preview {
    height: auto;
    max-height: 10;
    padding: 0 1;
    background: #0d1117;
    color: #c9d1d9;
    overflow-y: auto;
}

#stream-preview > .rich-markdown {
    /* Markdown 在预览区中的样式 */
}

/* 工具调用 Banner */
ToolCallBanner {
    height: auto;
    min-height: 1;
    padding: 0 1;
    margin: 1 0;
    background: #161b22;
}

ToolCallBanner.pending {
    border: dashed #30363d;
}

ToolCallBanner.running {
    border: solid #d29922;
}

ToolCallBanner.done {
    border: solid #238636;
}

ToolCallBanner.cache {
    border: solid #58a6ff;
}

/* 推理内容块（在 RichLog 中，通过 Rich markup 控制） */
/* 注意：推理块使用 Rich Panel 样式渲染，非独立 CSS */
```

---

### 2.6 验证

#### 功能测试

1. **增量流式**: 发送 "写一段 200 字的 Python 教程"，观察文本是否逐字出现（而非一次性刷新）
2. **工具提前通知**: 发送 "读取 README.md"，观察 "ReadTool" 是否在参数显示之前出现
3. **工具 banner**: 工具执行的 banner 是否正确显示：PENDING → RUNNING → DONE 状态转换
4. **缓存命中**: 发送两次相同的 "读取 README.md"，第二次应显示缓存命中标记
5. **推理内容**: 发送一条需要推理的问题（DeepSeek），观察 thinking 内容是否折叠显示
6. **取消中途**: 工具执行中按 Esc，banner 不应残留
7. **回归**: 所有 `/` 命令正常工作，`/clear` 清空流式缓冲区

#### 视觉效果检查

- [ ] 流式文本预览区在消息日志下方平滑更新
- [ ] 工具 banner 颜色编码正确（读=蓝边框，写=黄边框）
- [ ] 推理内容使用 dim 样式，与正常回复区分明显
- [ ] 终端缩放到 80x24 时流式预览不溢出

---

## 风险与缓解

| 风险                                                                   | 概率 | 影响 | 缓解                                                                      |
| ---------------------------------------------------------------------- | ---- | ---- | ------------------------------------------------------------------------- |
| Anthropic `content_block_start` 事件在 `AsyncAnthropic` 下行为不同 | 低   | 中   | 查阅 Anthropic Python SDK 文档确认 async stream 事件类型                  |
| 流式预览 Static 控件高度计算不准确                                     | 中   | 低   | `height: auto; max-height: 10` 提供弹性空间                             |
| ToolCallBanner 渲染与 RichLog Markdown 混排出现错位                    | 中   | 中   | RichLog.write(Widget) 可能不支持嵌套 Widget；备用方案为纯 Rich renderable |
| 推理内容积累内存增长（长推理）                                         | 低   | 低   | 推理内容限制 5000 字符，超出截断                                          |

---

## 完成标准

- [ ] 文本逐字渐进渲染（50ms 节流），而非批次刷新
- [ ] LLM 流中 tool name 出现时即刻通知 UI
- [ ] ToolCallBanner 正确显示 PENDING → RUNNING → DONE 生命周期
- [ ] 工具结果可展开/折叠
- [ ] 缓存命中显示特殊标记
- [ ] DeepSeek reasoning 内容以折叠块显示
- [ ] Ctrl+R 切换推理可见性
- [ ] 所有 `/` 命令、Esc 取消无回归
