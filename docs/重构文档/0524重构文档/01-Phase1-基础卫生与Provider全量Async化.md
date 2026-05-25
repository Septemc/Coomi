# Phase 1: 基础卫生 + Provider 全量 Async 化

> **状态**: 待执行 | **优先级**: P0（所有后续阶段的前置依赖） | **预计工作量**: 2-3 天
>
> **目标**: 架构根基重铸。删除死代码，Provider 全量 async 化消除 ThreadPoolExecutor 桥接，建立类型化事件体系。本阶段完成后的代码必须是 **可运行、零功能回归** 的。

---

## 前置背景

### 为什么要做这个阶段

当前项目存在以下根基性问题，阻塞了所有上层 UI 改进：

1. **旧 CLI 代码残留** — `apps/backend/cli/` 中的 typer CLI 完全绕过 AgentLoop，无工具调用、无会话管理，是死代码
2. **Provider 全同步** — 所有 LLM 调用是同步 generator，通过 `ThreadPoolExecutor` + `asyncio.Queue` 桥接到 async 事件循环。每次 LLM 流式调用占用一个线程池线程，架构不干净且浪费资源
3. **事件分发字符串化** — `AgentLoop.run_stream()` 用 `yield {"type": "tool_start", ...}` 这种裸 dict 传递事件，UI 侧用长 `if/elif` 链分发。无类型安全，无 IDE 补全，新增事件类型时容易遗漏
4. **BaseTool AB 契约谎言** — ABC 声明 `async def run`，15 个实现全部写 `def run`（同步），类型检查器会报错但运行时碰巧能用
5. **tool_formatter 游离** — 已实现但未被 Textual UI 调用，内联手写 `json.dumps`

### 本阶段不做什么

- 不改变任何用户可见行为（Phase 2 才做）
- 不新增 UI 控件
- 不改变工具功能
- 不碰上下文压缩/记忆系统逻辑（仅适配 async 调用签名）

---

## 详细执行计划

### 1.1 删除旧 CLI 和冗余代码

#### 1.1.1 删除文件清单

```
删除: apps/backend/cli/__init__.py
删除: apps/backend/cli/main.py
删除: apps/backend/cli/          (空目录)
删除: run_textual.py            (与 run.py 完全相同)
删除: apps/backend/api/__init__.py
删除: apps/backend/api/routes/__init__.py
删除: apps/backend/api/schemas/__init__.py
删除: apps/backend/api/          (空目录)
```

#### 1.1.2 修改 `pyproject.toml`

**删除所有 CLI/API 入口点：**

```toml
# 删除这行
coomi = "apps.backend.cli.main:app"

# 删除这行
coomi-tui = "run_textual:main"
```

**删除无用依赖（dependencies 段）：**

```toml
# 删除
"fastapi>=0.104.0",
"uvicorn[standard]>=0.24.0",
"typer>=0.9.0",
"aiofiles>=23.2.0",
```

**保留的 dependencies：**
```toml
dependencies = [
    "openai>=1.6.0",
    "anthropic>=0.104.0",
    "pydantic>=2.5.0",
    "python-dotenv>=1.0.0",
    "rich>=13.0.0",
    "textual>=1.0.0",
]
```

#### 1.1.3 修改 `requirements.txt`

同上，删除 `fastapi`、`uvicorn[standard]`、`typer`、`aiofiles`。

#### 1.1.4 全局搜索残留引用

执行以下检查，确保无遗漏：

```bash
# 确认无 typer 引用
rg "import typer|from typer" --type py apps/

# 确认无 fastapi 引用
rg "import fastapi|from fastapi" --type py apps/

# 确认无 uvicorn 引用
rg "import uvicorn|from uvicorn" --type py apps/

# 确认无旧 CLI 引用
rg "apps\.backend\.cli" --type py apps/
```

---

### 1.2 Provider 全量 Async 化

这是本阶段最大的改动，涉及 6 个 LLM 文件 + AgentLoop + async_utils + 记忆服务。

#### 1.2.1 改 `provider.py` — 抽象基类

**文件**: `apps/backend/core/services/llm/provider.py`

**修改前**（当前签名）:
```python
from typing import Iterator

class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages, tools=None, **kwargs) -> LLMResponse: ...

    @abstractmethod
    def chat_stream(self, messages, **kwargs) -> Iterator[str]: ...

    @abstractmethod
    def chat_stream_with_tools(self, messages, tools=None, **kwargs) -> Iterator[dict]: ...

    @abstractmethod
    def switch_model(self, model_name: str) -> str: ...

    @abstractmethod
    def get_model_display_name(self) -> str: ...
```

**修改后**:
```python
from typing import AsyncIterator

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse: ...

    @abstractmethod
    async def chat_stream(self, messages, **kwargs) -> AsyncIterator[str]: ...

    @abstractmethod
    async def chat_stream_with_tools(self, messages, tools=None, **kwargs) -> AsyncIterator[dict]: ...

    @abstractmethod
    def switch_model(self, model_name: str) -> str: ...
    # ↑ 保持同步 — switch_model 是纯内存操作，无需 async

    @abstractmethod
    def get_model_display_name(self) -> str: ...
    # ↑ 保持同步
```

#### 1.2.2 改 `generic.py` — GenericOpenAIProvider

**文件**: `apps/backend/core/services/llm/generic.py`

**核心变更**:

1. **客户端初始化**: `openai.OpenAI(...)` → `openai.AsyncOpenAI(...)`

```python
# 修改前
import openai

class GenericOpenAIProvider(LLMProvider):
    def __init__(self, ...):
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

# 修改后
import openai

class GenericOpenAIProvider(LLMProvider):
    def __init__(self, ...):
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
```

2. **`chat()` 方法**: 加 `async`，加 `await`

```python
# 修改前
def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
    params = self._build_params(messages, tools, stream=False)
    response = self.client.chat.completions.create(**params)
    return self._parse_response(response)

# 修改后
async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
    params = self._build_params(messages, tools, stream=False)
    response = await self.client.chat.completions.create(**params)
    return self._parse_response(response)
```

3. **`chat_stream()` 方法**: `def` → `async def`，`for` → `async for`，返回 `AsyncIterator`

```python
# 修改前
def chat_stream(self, messages, **kwargs) -> Iterator[str]:
    params = self._build_params(messages, tools=None, stream=True)
    response = self.client.chat.completions.create(**params)
    for chunk in response:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content

# 修改后
async def chat_stream(self, messages, **kwargs) -> AsyncIterator[str]:
    params = self._build_params(messages, tools=None, stream=True)
    response = await self.client.chat.completions.create(**params)
    async for chunk in response:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content
```

4. **`chat_stream_with_tools()` 方法** — 同样的模式转换：

```python
# 修改后
async def chat_stream_with_tools(self, messages, tools=None, **kwargs) -> AsyncIterator[dict]:
    params = self._build_params(messages, tools, stream=True)
    response = await self.client.chat.completions.create(**params)

    tool_calls_accum: dict[int, dict] = {}
    usage_chunk = None

    async for chunk in response:
        delta = chunk.choices[0].delta
        # ... tool call accumulation logic 保持不变 ...
        if delta.content:
            yield {"type": "content", "content": delta.content}
        elif model_extra.get("reasoning_content"):
            yield {"type": "reasoning_content", "content": model_extra["reasoning_content"]}

    # 流结束后 yield 累积的 tool calls 和 usage（保持不变）
    for tc in tool_calls_accum.values():
        yield {"type": "tool_call", "data": tc}
    if usage_chunk:
        yield {"type": "usage", "data": usage_chunk}
```

5. **`_build_params()` 和 `_parse_response()`**: 纯函数，不变

#### 1.2.3 改 `openai.py` — OpenAIProvider

**文件**: `apps/backend/core/services/llm/openai.py`

与 `generic.py` 模式完全相同。`OpenAIProvider` 不继承 `GenericOpenAIProvider`（它直接继承 `LLMProvider`），所以需要独立改造。

1. `self.client = openai.AsyncOpenAI(...)`
2. 所有方法加 `async`/`await`
3. 所有 `for chunk in response` → `async for chunk in response`
4. 返回类型 `Iterator` → `AsyncIterator`

#### 1.2.4 改 `deepseek.py` — DeepSeekProvider

**文件**: `apps/backend/core/services/llm/deepseek.py`

`DeepSeekProvider` 继承自 `GenericOpenAIProvider`。父类改为 async 后，子类只需：

1. Override 方法的签名同步改为 async（`async def`）
2. 内部调用 `await super().xxx()` 替代 `super().xxx()`

```python
# 修改前
def _build_params(self, messages, tools=None, stream=False):
    params = super()._build_params(messages, tools, stream)
    # DeepSeek 特有逻辑...
    return params

# 修改后
def _build_params(self, messages, tools=None, stream=False):
    # _build_params 是纯函数，保持同步不变
    params = super()._build_params(messages, tools, stream)
    return params
```

`_build_params` 是纯同步数据组装，不需要改。`chat_stream` 和 `chat_stream_with_tools` 如果有 override，加 `async` 即可。

#### 1.2.5 改 `anthropic.py` — AnthropicProvider

**文件**: `apps/backend/core/services/llm/anthropic.py`

**改动点**:

1. 客户端: `anthropic.Anthropic()` → `anthropic.AsyncAnthropic()`

```python
# 修改前
import anthropic
self.client = anthropic.Anthropic(api_key=api_key)

# 修改后
import anthropic
self.client = anthropic.AsyncAnthropic(api_key=api_key)
```

2. `chat()` 方法:

```python
# 修改前
def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
    anthropic_messages, system_prompt = self._convert_messages(messages)
    anthropic_tools = self._convert_tools(tools)
    response = self.client.messages.create(
        model=self.model,
        system=system_prompt,
        messages=anthropic_messages,
        tools=anthropic_tools,
        max_tokens=8192,
    )
    # ... parse response ...

# 修改后
async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
    anthropic_messages, system_prompt = self._convert_messages(messages)
    anthropic_tools = self._convert_tools(tools)
    response = await self.client.messages.create(
        model=self.model,
        system=system_prompt,
        messages=anthropic_messages,
        tools=anthropic_tools,
        max_tokens=8192,
    )
    # ... parse response (same logic) ...
```

3. `chat_stream_with_tools()`: Anthropic 使用 `client.messages.stream()` — 改用 async 版本：

```python
# 修改后
async def chat_stream_with_tools(self, messages, tools=None, **kwargs) -> AsyncIterator[dict]:
    anthropic_messages, system_prompt = self._convert_messages(messages)
    anthropic_tools = self._convert_tools(tools)

    async with self.client.messages.stream(
        model=self.model,
        system=system_prompt,
        messages=anthropic_messages,
        tools=anthropic_tools,
        max_tokens=8192,
    ) as stream:
        # tool use 状态机保持不变
        tool_inputs: dict[int, dict] = {}
        async for event in stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    yield {"type": "content", "content": event.delta.text}
                elif event.delta.type == "input_json_delta":
                    # ... 累积 tool input ...
                    pass
            elif event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    # ... tool use 开始 ...
                    pass

        # 流结束后 yield tool calls
        for idx, tc in tool_inputs.items():
            yield {"type": "tool_call", "data": tc}

        # yield usage
        final = await stream.get_final_message()
        yield {"type": "usage", "data": {
            "prompt_tokens": final.usage.input_tokens,
            "completion_tokens": final.usage.output_tokens,
            "total_tokens": final.usage.input_tokens + final.usage.output_tokens,
        }}
```

4. `_convert_messages()` 和 `_convert_tools()`: 纯函数，不变

#### 1.2.6 改 `llm.py` — LLMService 薄封装

**文件**: `apps/backend/core/services/llm/llm.py`

所有方法从同步 `def` + `Iterator` 改为 `async def` + `AsyncIterator`，内部 `await` provider 方法。这是一个纯代理层，改动简单。

#### 1.2.7 改 `factory.py` — Provider 工厂

**文件**: `apps/backend/core/services/llm/factory.py`

`get_llm_provider()` 和 `create_fast_provider()` 函数本身是同步的（只做实例化），不需要改为 async。但如果 `create_fast_provider()` 内部调用了 provider 方法，需要适配。

确认：`create_fast_provider()` 的当前实现是创建一个新的 provider 实例并切换 model — 纯同步操作。无需改动。

#### 1.2.8 改 `loop.py` — AgentLoop

**文件**: `apps/backend/core/engine/loop.py`

**核心简化** — 删除 `_chat_stream_via_bridge()` 方法，`run_stream()` 直接调用 async provider：

```python
# 修改前（lines 118-131）
async def _chat_stream_via_bridge(self, messages, tools=None):
    """通过 sync→async 桥接调用 LLM 流式 API"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            gen = sync_gen_to_async(self.llm.chat_stream_with_tools, messages, tools=tools)
            async for chunk in gen:
                yield chunk
            return
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    raise last_error

# 修改后 — 直接 async for，重试逻辑内联
# 在 run_stream() 中：
async for chunk in self.llm.chat_stream_with_tools(messages, tools=tools):
    # ... 处理 chunk ...
```

但需要保留重试逻辑。在 `run_stream()` 内部循环改为：

```python
# run_stream() 中的 LLM 调用部分
for retry in range(MAX_RETRIES):
    try:
        async for chunk in self.llm.chat_stream_with_tools(messages, tools=tools):
            # 取消检查
            if self._cancel_token.is_cancelled:
                yield AgentCancelled()
                return

            # 事件分发（Phase 1.4 会替换这些 dict 为 AgentEvent）
            if chunk["type"] == "reasoning_content":
                full_reasoning += chunk["content"]
            elif chunk["type"] == "content":
                full_content += chunk["content"]
                yield chunk["content"]  # 纯文本仍然 yield 字符串
            elif chunk["type"] == "tool_call":
                tool_calls_data.append(chunk["data"])
            elif chunk["type"] == "usage":
                update_token_usage(session, chunk["data"])
                yield UsageUpdate(chunk["data"])
        break  # 成功，跳出重试循环
    except Exception as e:
        last_error = e
        if retry < MAX_RETRIES - 1:
            await asyncio.sleep(2 ** retry)
        else:
            raise last_error
```

**同步 `run()` 方法**: 保留，内部用 `asyncio.run()` 桥接（供可能的非 async 调用方使用）：

```python
def run(self, session: Session, user_input: str) -> str:
    """同步封装 — 内部用 asyncio.run() 调用 run_stream()"""
    # 此处需要改为：收集 run_stream 的所有 yield，只返回最终文本
    # 或直接标记为 deprecated
    ...
```

> **决策**: 同步 `run()` 方法当前只被旧 CLI 使用（已删除），无人调用。可以标记为 deprecated 并在 Phase 4 移除，或简化为 `raise NotImplementedError`。Phase 1 保留但内部调用 `asyncio.run()` 桥接。

#### 1.2.9 改 `async_utils.py` — 简化

**文件**: `apps/backend/core/async_utils.py`

```python
# 修改后 — 删除 sync_gen_to_async，保留 run_in_thread

"""同步→异步桥接工具"""
from __future__ import annotations

import asyncio
from typing import Any


async def run_in_thread(func, *args: Any, **kwargs: Any) -> Any:
    """在线程池中执行同步函数，返回结果。用于工具执行。"""
    return await asyncio.to_thread(func, *args, **kwargs)
```

检查 `sync_gen_to_async` 的所有引用并移除 import：

```bash
rg "sync_gen_to_async" --type py apps/
```

预期引用位置：
- `apps/backend/core/engine/loop.py` — import 删除（已不再使用）
- `apps/backend/core/async_utils.py` — 函数定义删除

#### 1.2.10 改 `services/memory/extractor.py` 和 `recall.py`

**文件**: `apps/backend/core/services/memory/extractor.py`

如果 `MemoryExtractor.extract()` 内部调用了 `self.llm.chat(...)`，需要加 `await`：

```python
# 修改前
response = self.llm.chat(messages)

# 修改后
response = await self.llm.chat(messages)
```

**文件**: `apps/backend/core/services/memory/recall.py`

如果 `MemoryRecall.recall()` 内部调用了 `self.llm.chat(...)`，同样需要加 `await`。

`textual_app.py` 中调用 `self._memory_extractor.extract(...)` 的地方也需要检查是否需要 `await`。

#### 1.2.11 Provider Async 化验证清单

- [ ] `generic.py`: 所有 `for chunk in response` → `async for chunk in response`
- [ ] `generic.py`: 所有 `self.client.xxx.create()` → `await self.client.xxx.create()`
- [ ] `openai.py`: 同上
- [ ] `deepseek.py`: override 方法签名匹配 async 父类
- [ ] `anthropic.py`: `Anthropic()` → `AsyncAnthropic()`，`client.messages.stream()` → `async with`
- [ ] `llm.py`: 代理方法全部 `async def` + `await`
- [ ] `loop.py`: 删除 `_chat_stream_via_bridge`，直接 `async for`
- [ ] `async_utils.py`: 删除 `sync_gen_to_async`
- [ ] `extractor.py` / `recall.py`: LLM 调用加 `await`
- [ ] 无残留 `from .async_utils import sync_gen_to_async`
- [ ] 无残留 `sync_gen_to_async(` 调用

---

### 1.3 修复 BaseTool.run sync/async 不一致

#### 1.3.1 改 `base.py`

**文件**: `apps/backend/core/tools/base.py`

```python
# 修改前
@abstractmethod
async def run(self, arguments: dict[str, Any]) -> ToolResult:
    """执行工具"""
    ...

# 修改后
@abstractmethod
def run(self, arguments: dict[str, Any]) -> ToolResult:
    """执行工具（同步方法，由调用方通过 asyncio.to_thread 在线程池中执行）"""
    ...
```

**确认检查**: 所有 15 个工具实现的 `run()` 方法签名。

| 工具 | 文件 | run 签名 |
|------|------|---------|
| ReadTool | `tools/file_ops/read.py` | `def run` (sync) ✓ |
| WriteTool | `tools/file_ops/write.py` | `def run` (sync) ✓ |
| EditTool | `tools/file_ops/edit.py` | `def run` (sync) ✓ |
| GlobTool | `tools/search/glob.py` | `def run` (sync) ✓ |
| GrepTool | `tools/search/grep.py` | `def run` (sync) ✓ |
| BashTool | `tools/shell/bash.py` | `def run` (sync) ✓ |
| PowerShellTool | `tools/shell/powershell.py` | `def run` (sync) ✓ |
| WebFetchTool | `tools/web/fetch.py` | `def run` (sync) ✓ |
| WebSearchTool | `tools/web/search.py` | `def run` (sync) ✓ |
| TodoWriteTool | `tools/task/todo.py` | `def run` (sync) ✓ |
| AgentTool | `tools/agent/agent.py` | `def run` (sync) ✓ |
| AskUserQuestionTool | `tools/user/ask_question.py` | `def run` (sync) ✓ |
| EnterPlanModeTool | `tools/workspace/plan_mode.py` | `def run` (sync) ✓ |
| ExitPlanModeTool | `tools/workspace/plan_mode.py` | `def run` (sync) ✓ |
| ConfigTool | `tools/config/config.py` | `def run` (sync) ✓ |

**结论**: 所有实现已是 sync，修改 ABC 签名即可，零下游改动。

#### 1.3.2 改 `registry.py`

**文件**: `apps/backend/core/tools/registry.py`

```python
# 修改前 — execute_sync 检查 iscoroutinefunction
def execute_sync(self, tool_call: ToolCall) -> ToolResult:
    tool = self._tools[tool_call.name]
    if asyncio.iscoroutinefunction(tool.run):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(tool.run(tool_call.arguments))
        finally:
            loop.close()
    return tool.run(tool_call.arguments)

# 修改后 — 简化，直接调用
def execute_sync(self, tool_call: ToolCall) -> ToolResult:
    tool = self._tools[tool_call.name]
    return tool.run(tool_call.arguments)
```

`execute()` 方法（async）如果存在，改为用 `asyncio.to_thread` 包装：

```python
async def execute(self, tool_call: ToolCall) -> ToolResult:
    tool = self._tools[tool_call.name]
    return await asyncio.to_thread(tool.run, tool_call.arguments)
```

---

### 1.4 创建类型化事件系统

#### 1.4.1 新建 `events.py`

**文件**: `apps/backend/core/ui/events.py`

```python
"""Agent 事件类型 — AgentLoop 与 UI 之间的类型安全通信协议"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    """所有 Agent 事件的基类"""


@dataclass
class TextChunk(AgentEvent):
    """流式文本片段"""
    content: str


@dataclass
class ReasoningChunk(AgentEvent):
    """推理内容片段 (DeepSeek thinking mode)"""
    content: str


@dataclass
class ToolStart(AgentEvent):
    """工具即将开始执行"""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRunning(AgentEvent):
    """工具执行中"""
    tool_name: str


@dataclass
class ToolDone(AgentEvent):
    """工具执行完成"""
    tool_name: str
    elapsed: float  # 耗时（秒）


@dataclass
class ToolCacheHit(AgentEvent):
    """工具结果来自缓存"""
    tool_name: str


@dataclass
class UsageUpdate(AgentEvent):
    """Token 用量更新"""
    usage: dict[str, int]


@dataclass
class CompressionEvent(AgentEvent):
    """上下文压缩完成"""
    before: int  # 压缩前消息数
    after: int   # 压缩后消息数


@dataclass
class AgentError(AgentEvent):
    """Agent 执行错误"""
    message: str


@dataclass
class AgentCancelled(AgentEvent):
    """用户取消了 Agent 执行"""
```

#### 1.4.2 改 `loop.py` — 使用 AgentEvent

把所有 `yield {"type": "...", ...}` 替换为 `yield AgentEvent子类(...)`。

```python
# 修改前
yield {"type": "tool_start", "tool_name": tool_call.name, "arguments": tool_call.arguments}
yield {"type": "tool_cache_hit", "tool_name": tool_call.name}
yield {"type": "compression", "before": before_count, "after": len(compressed)}
yield {"type": "cancelled"}
yield {"type": "error", "message": "..."}

# 修改后
yield ToolStart(tool_name=tool_call.name, arguments=tool_call.arguments)
yield ToolCacheHit(tool_name=tool_call.name)
yield CompressionEvent(before=before_count, after=len(compressed))
yield AgentCancelled()
yield AgentError(message="...")
```

**`run_stream()` 新签名**:

```python
async def run_stream(
    self, session: Session, user_input: str
) -> AsyncIterator[str | AgentEvent]:
    """执行 Agent 主循环（异步流式输出）

    Yields:
        str: 纯文本内容片段（流式输出）
        AgentEvent 子类: 结构化事件（工具调用、用量更新、压缩、错误等）
    """
```

#### 1.4.3 改 `textual_app.py` — 事件分发

将 `_run_agent()` 中的长 `if/elif` 链替换为 `isinstance()` 模式匹配：

```python
# 修改前
if isinstance(chunk, str):
    self._stream_buffer += chunk
elif isinstance(chunk, dict):
    ct = chunk.get("type")
    if ct == "tool_start":
        ...
    elif ct == "tool_running":
        ...
    elif ct == "tool_done":
        ...
    # ... 8+ elif ...

# 修改后
if isinstance(chunk, str):
    self._stream_buffer += chunk
elif isinstance(chunk, ToolStart):
    tool_name = chunk.tool_name
    args_str = format_tool_display(tool_name, chunk.arguments)
    if len(args_str) > 150:
        args_str = args_str[:147] + "..."
    self._tool_start_time = time.time()
    self._pending_tool_name = tool_name
    self._pending_tool_args = args_str
    indicator.update(f"[bold yellow]⟳ {tool_name}...[/bold yellow]")
elif isinstance(chunk, ToolRunning):
    indicator.update(f"[bold yellow]⟳ {chunk.tool_name}...[/bold yellow]")
elif isinstance(chunk, ToolDone):
    elapsed = time.time() - self._tool_start_time if self._tool_start_time else 0
    name = self._pending_tool_name or chunk.tool_name
    self._wl(log, f"\n[bold yellow]{name}[/bold yellow] [dim]{self._pending_tool_args}[/dim] [dim]✓ ({elapsed:.1f}s)[/dim]")
    indicator.update("[bold yellow]◎ Thinking...[/bold yellow]")
elif isinstance(chunk, ToolCacheHit):
    name = self._pending_tool_name or chunk.tool_name
    self._wl(log, f"\n[bold yellow]{name}[/bold yellow] [dim]{self._pending_tool_args}[/dim] [dim]✓ cache[/dim]")
elif isinstance(chunk, UsageUpdate):
    self.status_line.update_usage(chunk.usage)
    status.refresh()
elif isinstance(chunk, CompressionEvent):
    status.set_compressing(chunk.before, chunk.after)
    self._wl(log, f"[dim]Context compressed: {chunk.before} -> {chunk.after} messages[/dim]")
elif isinstance(chunk, AgentCancelled):
    self._wl(log, "\n[dim]Cancelled.[/dim]")
    break
elif isinstance(chunk, AgentError):
    self._wl(log, f"\n[red]Error: {chunk.message}[/red]")
    break
```

---

### 1.5 接线 tool_formatter

**文件**: `apps/backend/core/ui/textual_app.py`

```python
# 修改前（lines 426-428）
args_str = json.dumps(args, ensure_ascii=False)
if len(args_str) > 150:
    args_str = args_str[:147] + "..."

# 修改后 — 使用已有的格式化函数
from .tool_formatter import format_tool_display
args_str = format_tool_display(tool_name, arguments)
```

> `format_tool_display` 已经能针对不同工具类型生成人类可读的字符串，如 `Read /path/file.py (lines 10-200)`、`Bash: git status` 等。

---

### 1.6 填充 widgets/__init__.py

**文件**: `apps/backend/core/ui/widgets/__init__.py`

```python
"""UI 控件"""
from .status_panel import StatusPanel

__all__ = ["StatusPanel"]
```

---

### 1.7 清理过期文档

#### 1.7.1 `status_line.py` 文档字符串

**文件**: `apps/backend/core/ui/status_line.py`

```python
# 修改前（lines 1-4）
"""状态栏 - 显示模型、上下文窗口、Token 使用情况

状态栏是纯数据持有层，不执行任何终端渲染。
所有渲染由 run.py 的 _status_bar() 函数通过 Live Layout 统一完成。
"""

# 修改后
"""状态栏 - 显示模型、上下文窗口、Token 使用情况

纯数据持有层，不执行任何渲染。所有渲染由 Textual 的 StatusPanel 控件统一完成。
"""
```

同时修改 line 72：

```python
# 修改前
self.estimated_prompt_tokens: int = 0  # 当前 prompt 估算（公开属性，供 _status_bar 读取）

# 修改后
self.estimated_prompt_tokens: int = 0  # 当前 prompt 估算（公开属性，供 StatusPanel 读取）
```

#### 1.7.2 `README.md`

架构图中删除 `stream_renderer.py` 引用：

```diff
- │       ├── stream_renderer.py  # Rich Live Markdown 流式渲染
  │       ├── status_line.py      # 状态栏（模型 + Token 用量）
```

#### 1.7.3 `docs/解析文档/项目概述.md`

```diff
-         ├─ stream_renderer.py      ← 瀑布流 Markdown 渲染器
```

在第 53 行附近删除对 `stream_renderer.py` 的引用。

---

### 1.8 验证

执行以下步骤，确保 Phase 1 完成后无回归：

#### 启动测试

```bash
python run.py
```

预期: Textual TUI 正常启动，显示欢迎信息。

#### 基本对话测试

```
> 你好，请用一句话介绍你自己
```

预期: Agent 正常响应，流式输出文本。

#### 工具调用测试

```
> 读取 README.md 的第一行
```

预期: Agent 调用 ReadTool，执行成功，显示工具名称和参数。

#### 命令测试

```
/model          → 显示可用模型列表
/context        → 显示当前上下文窗口
/context 512k   → 设置上下文窗口为 512K
/memory list    → 列出记忆
/clear          → 清空会话
```

#### 取消测试

发送一条长消息，在 Agent 执行中按 `Esc`。预期: 显示 "Cancelled."，输入恢复可用。

#### 代码检查

```bash
# 确认旧代码已删除
python -c "import apps.backend.cli"  # 应报 ModuleNotFoundError

# 确认无 typer 引用
rg "import typer|from typer" apps/

# 确认无 sync_gen_to_async 引用
rg "sync_gen_to_async" apps/

# 确认无残留 {"type": "tool_start" 形式的裸 dict 事件
rg '"type": "tool_start"' apps/

# 确认 asyncio.iscoroutinefunction 已移除
rg "iscoroutinefunction" apps/
```

#### 依赖检查

```bash
pip list | grep -E "typer|fastapi|uvicorn|aiofiles"
# 应无输出（如果虚拟环境中之前安装过，可能需要 pip uninstall）
```

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| Anthropic async stream API 行为与 sync 不同 | 中 | 高 — 工具调用解析失败 | 参考 Anthropic Python SDK 文档，确保 `async with client.messages.stream()` 的 tool use 状态机正确 |
| DeepSeek API 的 AsyncOpenAI 兼容性 | 低 | 中 — 流式响应格式可能不同 | DeepSeek 完全兼容 OpenAI SDK，已验证 `AsyncOpenAI(base_url="https://api.deepseek.com")` 可用 |
| MemoryExtractor / MemoryRecall 漏改 await | 中 | 低 — 启动时报 RuntimeWarning | 全局搜索 `self.llm.chat(` 确认所有调用点都加了 `await` |
| 同步 `run()` 方法被未知调用方使用 | 低 | 低 — 仅旧 CLI 使用 | `asyncio.run()` 桥接在已有 event loop 的上下文中会报错；如有问题改为 `NotImplementedError` |

---

## 完成标准

- [ ] `python run.py` 启动正常，无 import 错误
- [ ] Agent 对话正常（流式输出 + 工具调用）
- [ ] 所有 `/` 命令正常
- [ ] Esc 取消工作正常
- [ ] `import apps.backend.cli` 报 ModuleNotFoundError
- [ ] 项目中无 `typer`、`fastapi`、`uvicorn`、`aiofiles` 的 import
- [ ] `sync_gen_to_async` 函数定义和所有引用已删除
- [ ] `tools/base.py` 中 `run` 签名是 `def run`（非 `async def`）
- [ ] `loop.py` 使用 `AgentEvent` 子类 yield，无裸 dict `{"type": ...}`
- [ ] `textual_app.py` 使用 `isinstance()` 模式匹配分发事件
- [ ] `tool_formatter.py` 的 `format_tool_display()` 被 Textual app 调用
- [ ] `status_line.py` docstring 不包含 `_status_bar` 引用
