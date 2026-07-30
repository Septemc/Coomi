"""Agent主循环 - 核心引擎

生产级错误处理策略：
1. 循环检测: 同一工具同一参数连续失败时注入警告，强制 LLM 换方法
2. 双层计数: 有效迭代 vs 连续失败分开计数，失败不消耗主配额
3. LLM 降级: API 异常不崩溃，优雅降级为 AgentError 后正常结束
4. 工具崩溃隔离: 单个工具异常不影响整体循环
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from ..security import HookSystem, PermissionLevel, PermissionSystem
from ..services.context.compressor import ContextCompressor
from ..services.llm.provider import LLMProvider
from ..services.llm.text_tool_calls import TextToolCallFilter, is_likely_text_tool_call
from ..tools.base import ToolAccess, ToolConcurrency
from ..tools.registry import ToolRegistry
from ..types import ToolCall
from ..ui.events import (
    AgentCancelled,
    AgentError,
    AgentEvent,
    BackgroundTaskCompleted,
    BackgroundTaskDetached,
    CompressionEvent,
    ConnectionRetry,
    ReasoningChunk,
    TextChunk,
    ToolDone,
    ToolRunning,
    ToolStart,
    UsageUpdate,
)
from .background_tasks import BackgroundTaskRegistry
from .session import Session, add_assistant_message, add_tool_result, add_user_message, update_token_usage
from .tool_executor import ToolExecutionOutcome, ToolExecutor

MAX_ITERATIONS = 100           # 总迭代上限（有效迭代，不含连续失败重试）
MAX_RETRIES = 3                # LLM API 调用重试次数
MAX_CONSECUTIVE_FAILURES = 5   # 连续失败上限（达到后强制注入警告）
MAX_SAME_TOOL_CALL = 3         # 同一工具同一参数连续调用上限
LOOP_WARN_THRESHOLD = 3        # 循环检测警告阈值
LOOP_FORCE_BREAK_THRESHOLD = 5 # 循环检测强制中断阈值
MAX_CONSECUTIVE_LOW_INFO_RESULTS = 8
MAX_TOOL_CONCURRENCY = int(os.environ.get("COOMI_MAX_TOOL_CONCURRENCY", "10"))
MAX_MALFORMED_TEXT_TOOL_RETRIES = 3
logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_RETRYABLE_ERROR_MARKERS = (
    "aborted",
    "connection reset",
    "connection closed",
    "connection refused",
    "server disconnected",
    "remote protocol error",
    "incomplete chunked read",
    "empty stream",
    "network error",
    "temporarily unavailable",
    "timed out",
    "timeout",
)

_TOOL_INTENT_HINTS = (
    "file",
    "read",
    "write",
    "edit",
    "code",
    "repo",
    "project",
    "run",
    "execute",
    "test",
    "search",
    "web",
    "fetch",
    "url",
    "git",
    "shell",
    "cmd",
    "bash",
    "bug",
    "fix",
    "implement",
    "weather",
    "news",
    "latest",
    "current",
    "style",
    "design",
    "icon",
    "favicon",
    "logo",
    "image",
    "asset",
    "html",
    "css",
    "page",
    "ui",
    "replace",
    "\u6587\u4ef6",
    "\u8bfb",
    "\u5199",
    "\u7f16\u8f91",
    "\u4fee\u6539",
    "\u4ee3\u7801",
    "\u9879\u76ee",
    "\u8fd0\u884c",
    "\u6267\u884c",
    "\u6d4b\u8bd5",
    "\u641c\u7d22",
    "\u7f51\u9875",
    "\u8054\u7f51",
    "\u7f51\u7ad9",
    "\u5929\u6c14",
    "\u65b0\u95fb",
    "\u6700\u65b0",
    "\u5f53\u524d",
    "\u4fee\u590d",
    "\u5b9e\u73b0",
    "\u4f18\u5316",
    "\u8c03\u6574",
    "\u62a5\u9519",
    "\u9519\u8bef",
    "\u8bbe\u8ba1",
    "\u6837\u5f0f",
    "\u56fe\u6807",
    "\u6536\u85cf\u5939",
    "\u5706\u89d2",
    "\u9875\u9762",
    "\u754c\u9762",
    "\u56fe\u7247",
    "\u7d20\u6750",
    "\u94fe\u63a5",
    "\u66ff\u6362",
    "\u4f7f\u7528",
)


def _should_omit_tools_for_input(user_input: str) -> bool:
    text = " ".join((user_input or "").casefold().strip().split())
    if not text:
        return False
    if any(hint in text for hint in _TOOL_INTENT_HINTS):
        return False
    compact = "".join(text.split())
    return len(compact) <= 24


def _is_retryable_llm_error(error: Exception) -> bool:
    """Return whether an LLM failure is likely transient and safe to retry."""
    status_code = getattr(error, "status_code", None)
    if status_code in _RETRYABLE_STATUS_CODES:
        return True

    error_name = type(error).__name__.casefold()
    if any(
        marker in error_name
        for marker in ("connection", "connect", "timeout", "ratelimit", "internalserver")
    ):
        return True

    message = str(error).casefold()
    return any(marker in message for marker in _RETRYABLE_ERROR_MARKERS)


def _abort_only_stream(content: str, reasoning: str, had_tool_call: bool = False) -> str | None:
    """Detect provider-side abort sent as a tiny response instead of an exception."""
    if had_tool_call:
        return None
    combined = " ".join(part.strip() for part in (content, reasoning) if part.strip())
    normalized = " ".join(combined.casefold().split())
    if not normalized or len(normalized) > 240:
        return None
    if normalized in {"aborted", "error: aborted", "request aborted", "error: request aborted"}:
        return combined
    if normalized.startswith("error:") and any(
        marker in normalized for marker in _RETRYABLE_ERROR_MARKERS
    ):
        return combined
    return None


class CancelToken:
    """异步取消令牌"""

    def __init__(self):
        self._event = asyncio.Event()
        self._input_buffer: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()
        self._input_buffer = None

    def set_input_buffer(self, text: str) -> None:
        self._input_buffer = text

    def get_input_buffer(self) -> str | None:
        return self._input_buffer

    async def wait_cancelled(self) -> None:
        """等待取消信号被置位（供工具执行与取消竞速）。"""
        await self._event.wait()


class LoopDetector:
    """检测 LLM↔工具 的死循环

    策略：
    - 记录最近 N 次工具调用的 (tool_name, args_hash)
    - 连续 LOOP_WARN_THRESHOLD 次相同调用 → 注入警告消息
    - 连续 LOOP_FORCE_BREAK_THRESHOLD 次相同调用 → 强制中断，注入换方法提示
    """

    def __init__(self, window_size: int = 10):
        self._history: list[tuple[str, str]] = []
        self._window_size = window_size

    @staticmethod
    def _args_key(arguments: dict) -> str:
        """生成参数的稳定哈希键"""
        try:
            return json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(arguments)

    def record(self, tool_name: str, arguments: dict) -> int:
        """记录工具调用，返回连续相同调用次数"""
        key = (tool_name, self._args_key(arguments))
        self._history.append(key)
        if len(self._history) > self._window_size:
            self._history.pop(0)

        # 计算最近连续相同调用次数
        count = 0
        for past in reversed(self._history):
            if past == key:
                count += 1
            else:
                break
        return count

    def is_stuck(self) -> bool:
        """检测是否陷入死循环"""
        if len(self._history) < 3:
            return False
        # 最近 window 内全部相同
        return len(set(self._history[-3:])) == 1

    def reset(self) -> None:
        """重置检测器"""
        self._history.clear()


def _is_low_information_tool_result(result_text: str) -> bool:
    normalized = " ".join((result_text or "").casefold().split())
    if not normalized:
        return True
    low_info_markers = (
        "(tool completed with no output)",
        "no matches found",
        "no files found",
        "permission required for tool",
        "permission denied for tool",
        "plan mode is active:",
        "invalid json arguments",
    )
    return any(marker in normalized for marker in low_info_markers)


class AgentLoop:
    """Agent主循环 - 协调LLM调用和工具执行

    核心流程：
    1. 拼接消息（系统提示 + 历史 + 用户输入）
    2. 调用LLM
    3. 如果有工具调用 → 执行工具 → 继续循环
    4. 如果无工具调用 → 返回结果

    生产级错误处理：
    - 循环检测：同一工具同一参数连续失败时注入警告
    - 双层计数：有效迭代和连续失败分开统计
    - LLM 降级：API 异常优雅降级
    - 工具崩溃隔离：单个工具异常不影响整体
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        context_window_size: int = 256_000,
        app_context: Any = None,
        permission_system: PermissionSystem | None = None,
        hook_system: HookSystem | None = None,
        project_path: str | None = None,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_window_size = context_window_size
        self.compressor = ContextCompressor(llm)
        self._cancel_token = CancelToken()
        self.app_context = app_context  # CoomiApp 实例，交互式工具需要
        self.permission_system = permission_system or PermissionSystem()
        self.hook_system = hook_system or HookSystem()
        self.project_path = project_path or os.getcwd()
        self.tool_executor = ToolExecutor(
            tool_registry,
            permission_system=self.permission_system,
            hook_system=self.hook_system,
            app_context=app_context,
            project_path=self.project_path,
        )
        self._plan_mode: bool = False
        self._loop_detector = LoopDetector()
        self._background_tasks = BackgroundTaskRegistry()

    @property
    def background_tasks(self) -> BackgroundTaskRegistry:
        return self._background_tasks

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    def set_plan_mode(self, active: bool) -> None:
        self._plan_mode = active
        self.tool_executor.read_only_mode = active

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel_token

    async def _execute_tool_async(
        self,
        session: Session,
        tool_call: ToolCall,
    ) -> ToolExecutionOutcome:
        """异步执行工具调用

        Returns:
            ToolExecutionOutcome
        """
        return await self.tool_executor.execute(session, tool_call)

    @staticmethod
    def _discard_task(task: "asyncio.Task[ToolExecutionOutcome]") -> None:
        """丢弃一个不再需要结果的工具任务，吞掉其异常避免 "never retrieved" 告警。

        「停止真杀」路径用它：子进程已被 kill，工具线程会很快返回，
        我们不关心其结果，只需确保异常不冒泡。
        """
        def _swallow(t: "asyncio.Task[ToolExecutionOutcome]") -> None:
            if not t.cancelled():
                _ = t.exception()

        task.add_done_callback(_swallow)

    def _partition_tool_calls(self, tool_calls: list[ToolCall]) -> list[list[ToolCall]]:
        """Group consecutive concurrency-safe tool calls into parallel batches."""
        batches: list[list[ToolCall]] = []
        current_parallel: list[ToolCall] = []

        for tool_call in tool_calls:
            if self._can_run_in_parallel(tool_call):
                current_parallel.append(tool_call)
                if len(current_parallel) >= MAX_TOOL_CONCURRENCY:
                    batches.append(current_parallel)
                    current_parallel = []
                continue

            if current_parallel:
                batches.append(current_parallel)
                current_parallel = []
            batches.append([tool_call])

        if current_parallel:
            batches.append(current_parallel)

        return batches

    def _can_run_in_parallel(self, tool_call: ToolCall) -> bool:
        if tool_call.parse_error:
            return False
        tool = self.tool_registry.get(tool_call.name)
        if tool is None or tool.is_interactive:
            return False
        if tool.concurrency != ToolConcurrency.PARALLEL:
            return False
        permission = self.permission_system.check_execution_permission(
            tool.name,
            tool_call.arguments,
            source=tool_call.source,
            mutates_state=tool.access in {ToolAccess.WRITE, ToolAccess.DESTRUCTIVE},
        )
        return permission == PermissionLevel.AUTO

    def _canonical_tool_name(self, name: str) -> str:
        return self.tool_registry.canonical_name(name) or name

    async def _check_compress(self, session: Session) -> CompressionEvent | None:
        """检查是否需要压缩，需要时执行并返回事件"""
        if self.compressor.should_compress(session, self.context_window_size):
            before_count = len(session.messages)
            compressed = await self.compressor.compress(session, self.context_window_size)
            session.messages = compressed
            return CompressionEvent(before=before_count, after=len(compressed))
        return None

    async def _chat_with_retry(self, messages, tools=None):
        """带重试的 LLM 调用"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self.llm.chat(messages, tools=tools)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    async def _chat_stream_with_retry(self, messages, tools=None):
        """Retry transient stream failures before content or tool calls are committed."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            committed_output = False
            attempt_content: list[str] = []
            attempt_reasoning: list[str] = []
            had_tool_call = False
            try:
                async for chunk in self.llm.chat_stream_with_tools(messages, tools=tools):
                    chunk_type = chunk.get("type")
                    if chunk_type == "content":
                        attempt_content.append(str(chunk.get("content") or ""))
                        committed_output = True
                    elif chunk_type == "reasoning_content":
                        attempt_reasoning.append(str(chunk.get("content") or ""))
                    elif chunk_type in {"tool_call", "tool_call_start"}:
                        committed_output = True
                        had_tool_call = True
                    yield chunk

                content_text = "".join(attempt_content)
                reasoning_text = "".join(attempt_reasoning)
                abort_message = _abort_only_stream(
                    content_text,
                    reasoning_text,
                    had_tool_call=had_tool_call,
                )
                if abort_message:
                    raise RuntimeError(f"Transient stream aborted: {abort_message}")
                if not had_tool_call and not content_text.strip() and not reasoning_text.strip():
                    raise RuntimeError("Transient empty stream response")
                return  # 成功完成
            except Exception as e:
                last_error = e
                abort_only = _abort_only_stream(
                    "".join(attempt_content),
                    "".join(attempt_reasoning),
                    had_tool_call=had_tool_call,
                )
                can_retry = (
                    attempt < MAX_RETRIES - 1
                    and _is_retryable_llm_error(e)
                    and (not committed_output or abort_only is not None)
                )
                if not can_retry:
                    raise last_error
                delay = float(2 ** attempt)
                yield {
                    "type": "connection_retry",
                    "attempt": attempt + 2,
                    "max_attempts": MAX_RETRIES,
                    "delay": delay,
                    "message": str(e),
                }
                await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    def _build_loop_warning(self, tool_name: str, consecutive_count: int, is_stuck: bool) -> str:
        """构建循环检测警告消息，注入到 tool_result 中"""
        if is_stuck:
            return (
                f"\n\n⚠️ [LOOP DETECTED] 你已连续 {consecutive_count} 次调用 {tool_name} 且结果相同。"
                f"这表明当前方法无效。\n"
                f"**你必须立即换一种完全不同的方法来解决这个问题。**\n"
                f"- 如果是命令报错，仔细阅读错误信息，修改命令后重试\n"
                f"- 如果是代码问题，先分析根因再修改，不要盲目重试\n"
                f"- 如果无法解决，请向用户说明情况并请求帮助\n"
            )
        elif consecutive_count >= LOOP_WARN_THRESHOLD:
            return (
                f"\n\n⚠️ [WARNING] 你已连续 {consecutive_count} 次调用 {tool_name}。"
                f"如果继续使用相同方法，可能会陷入死循环。"
                f"请考虑换一种不同的方法。"
            )
        return ""

    def _build_force_break_message(self, tool_name: str, consecutive_count: int) -> str:
        """构建强制中断时的提示消息"""
        return (
            f"⚠️ Agent 检测到工具 {tool_name} 已连续调用 {consecutive_count} 次，"
            f"结果始终相同。当前方法无法解决问题，已强制中断循环。\n\n"
            f"建议：\n"
            f"1. 仔细分析之前的错误信息，找出根本原因\n"
            f"2. 尝试完全不同的方法或命令\n"
            f"3. 如果需要，可以向用户请求更多信息\n\n"
            f"请告诉我你打算如何继续。"
        )

    async def run_stream(self, session: Session, user_input: str) -> AsyncIterator[AgentEvent]:
        """执行Agent主循环（异步流式输出）

        生产级错误处理：
        - 双层计数：有效迭代 vs 连续失败
        - 循环检测：同一工具同一参数连续调用时注入警告
        - LLM 异常降级：API 调用失败不崩溃
        - 工具崩溃隔离：单个工具异常不终止循环

        Yields:
            AgentEvent 子类: 结构化事件（文本、工具调用、用量更新、压缩、错误等）
        """
        add_user_message(session, user_input)
        self._cancel_token.reset()
        self._loop_detector.reset()

        compress_event = await self._check_compress(session)
        if compress_event:
            yield compress_event

        effective_iteration = 0       # 有效迭代计数
        consecutive_failures = 0      # 连续失败计数（独立于有效迭代）
        consecutive_low_info_results = 0
        total_tool_calls = 0          # 总工具调用次数
        total_tool_errors = 0         # 总工具错误次数
        omit_tools_for_first_turn = _should_omit_tools_for_input(user_input)

        malformed_text_tool_retries = 0

        while effective_iteration < MAX_ITERATIONS:
            # 取消检查点
            if self._cancel_token.is_cancelled:
                yield AgentCancelled()
                return

            effective_iteration += 1

            # ---- 后台任务结果回灌 ----
            # 被「插队 detach」转入后台的工具若已完成，将其结果作为新的 user
            # 消息注入，让本轮 LLM 立刻看到迟到的结果并接续。
            for bg in self._background_tasks.drain_completed():
                add_user_message(
                    session,
                    f"[后台任务 #{bg.task_id} 完成] 工具 {bg.tool_name} 返回：\n{bg.result_text}",
                )
                yield BackgroundTaskCompleted(
                    task_id=bg.task_id,
                    tool_name=bg.tool_name,
                    is_error=bg.is_error,
                )

            messages = session.get_messages_for_api()
            available_tools = self.tool_registry.get_tool_definitions() or None
            if effective_iteration == 1 and omit_tools_for_first_turn:
                tools = None
            else:
                tools = available_tools

            full_content = ""
            full_reasoning = ""
            tool_calls_data = []
            # Textual tool-call fallback must remain active whenever tools exist.
            # Some providers emit DSML/XML/JSON tool calls as plain content even
            # when the first request optimizes native tool definitions away.
            text_tool_mode = self.llm.get_text_tool_mode() if available_tools else "disabled"
            text_tool_filter = TextToolCallFilter(mode=text_tool_mode)
            reasoning_tool_filter = TextToolCallFilter(mode=text_tool_mode)

            # ---- LLM API 调用（含降级处理） ----
            try:
                async for chunk in self._chat_stream_with_retry(messages, tools=tools):
                    # 流式过程中也检查取消
                    if self._cancel_token.is_cancelled:
                        yield AgentCancelled()
                        return

                    if chunk["type"] == "reasoning_content":
                        if text_tool_mode == "disabled" and is_likely_text_tool_call(chunk["content"], mode="structured"):
                            logger.warning(
                                "Text tool parsing is disabled while model reasoning looks like a tool call. "
                                "Check provider tool_protocol/text_tool_mode configuration."
                            )
                        visible_reasoning, parsed_text_calls = reasoning_tool_filter.feed(chunk["content"])
                        tool_calls_data.extend(parsed_text_calls)
                        if visible_reasoning:
                            full_reasoning += visible_reasoning
                            yield ReasoningChunk(content=visible_reasoning)
                    elif chunk["type"] == "tool_call_start":
                        yield ToolStart(
                            tool_name=self._canonical_tool_name(chunk["tool_name"]),
                            arguments={},
                        )
                    elif chunk["type"] == "content":
                        if text_tool_mode == "disabled" and is_likely_text_tool_call(chunk["content"], mode="structured"):
                            logger.warning(
                                "Text tool parsing is disabled while model content looks like a tool call. "
                                "Check provider tool_protocol/text_tool_mode configuration."
                            )
                        visible_content, parsed_text_calls = text_tool_filter.feed(chunk["content"])
                        tool_calls_data.extend(parsed_text_calls)
                        if visible_content:
                            full_content += visible_content
                            yield TextChunk(content=visible_content)
                    elif chunk["type"] == "tool_call":
                        tool_calls_data.append(chunk["data"])
                    elif chunk["type"] == "usage":
                        update_token_usage(session, chunk["data"])
                        yield UsageUpdate(usage=chunk["data"])
                    elif chunk["type"] == "connection_retry":
                        # Discard the failed attempt's uncommitted response and reset
                        # streaming parsers before reconnecting.
                        full_content = ""
                        full_reasoning = ""
                        tool_calls_data = []
                        text_tool_filter = TextToolCallFilter(mode=text_tool_mode)
                        reasoning_tool_filter = TextToolCallFilter(mode=text_tool_mode)
                        yield ConnectionRetry(
                            attempt=chunk["attempt"],
                            max_attempts=chunk["max_attempts"],
                            delay=chunk["delay"],
                            message=chunk["message"],
                        )
            except Exception as e:
                # P0-C: LLM API 异常降级 — 不崩溃，优雅退出当前 run
                if _abort_only_stream(full_content, full_reasoning):
                    full_content = ""
                    full_reasoning = ""
                source_file = os.path.abspath(__file__)
                yield AgentError(
                    message=(
                        f"LLM API 调用失败: {type(e).__name__}: {e}\n"
                        f"  来源文件: {source_file}\n"
                        f"  会话ID: {session.id}\n"
                        f"  有效迭代: {effective_iteration}\n"
                        f"  工具调用: {total_tool_calls} (错误: {total_tool_errors})\n"
                        f"你的会话状态已保存，可以在恢复后继续对话。"
                    ),
                    is_fatal=False,  # LLM 降级错误，用户可继续对话
                )
                # 注入一条 assistant 消息，让用户可以继续对话
                add_assistant_message(
                    session,
                    f"[系统] LLM API 调用暂时失败 ({type(e).__name__})。你可以继续输入，我会重试。",
                    reasoning_content=full_reasoning or None,
                )
                return  # 正常结束当前 run，不抛异常

            tail_reasoning, parsed_text_calls = reasoning_tool_filter.flush()
            tool_calls_data.extend(parsed_text_calls)
            if tail_reasoning:
                full_reasoning += tail_reasoning
                yield ReasoningChunk(content=tail_reasoning)

            tail_content, parsed_text_calls = text_tool_filter.flush()
            tool_calls_data.extend(parsed_text_calls)
            if tail_content:
                full_content += tail_content
                yield TextChunk(content=tail_content)

            if tool_calls_data:
                tool_calls = [
                    ToolCall(
                        id=tc["id"] or f"call_{i}_{id(tc)}",
                        name=self._canonical_tool_name(tc["name"]),
                        arguments=tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {},
                        raw_arguments=tc.get("raw_arguments"),
                        parse_error=tc.get("parse_error"),
                        source=tc.get("source", "native"),
                    )
                    for i, tc in enumerate(tool_calls_data)
                ]
                reasoning = full_reasoning or None
                add_assistant_message(
                    session,
                    full_content if full_content.strip() else None,
                    tool_calls,
                    reasoning,
                )

                force_break = False
                stop_for_no_progress = False
                stop_for_malformed_text_tools = False

                for batch in self._partition_tool_calls(tool_calls):
                    for tool_call in batch:
                        total_tool_calls += 1
                        yield ToolStart(tool_name=tool_call.name, arguments=tool_call.arguments)
                        yield ToolRunning(tool_name=tool_call.name)

                    if len(batch) == 1:
                        # 单工具（含阻塞式 PowerShell/Bash）：让工具执行与取消信号竞速，
                        # 以便「插队」能立即中断等待、把工具转入后台，主流接续。
                        tool_task = asyncio.ensure_future(
                            self._execute_tool_async(session, batch[0])
                        )
                        cancel_waiter = asyncio.ensure_future(
                            self._cancel_token.wait_cancelled()
                        )
                        await asyncio.wait(
                            {tool_task, cancel_waiter},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if tool_task.done():
                            # 工具先完成 —— 正常路径
                            cancel_waiter.cancel()
                            outcomes = [tool_task.result()]
                        else:
                            # 取消先到 —— 区分「插队 detach」与「停止真杀」
                            cancel_waiter.cancel()
                            tc = batch[0]
                            buffered = self._cancel_token.get_input_buffer()
                            if buffered and self._background_tasks.can_detach():
                                # 插队：工具转入后台（子进程继续跑），写占位 tool_result
                                # 满足消息配对，随后返回让 UI 拾取插队内容重启一轮。
                                task_id = self._background_tasks.detach(
                                    tc.id, tc.name, tool_task
                                )
                                add_tool_result(
                                    session,
                                    tc.id,
                                    f"⏳ [后台任务 #{task_id}] 工具 {tc.name} 仍在后台执行，"
                                    f"完成后其结果会以系统消息回灌。请先处理用户的新指令。",
                                )
                                yield BackgroundTaskDetached(
                                    task_id=task_id, tool_name=tc.name
                                )
                                return
                            if buffered:
                                # 插队但后台槽位已满（已有 1 个后台任务）：无法再转后台，
                                # 退回正常等待工具完成；插队内容仍留在 buffer，由 UI 在
                                # 本轮结束时拾取。不走真杀。
                                outcomes = [await tool_task]
                            else:
                                # 停止真杀：子进程已由 UI 层 kill，丢弃工具任务结果避免告警。
                                self._discard_task(tool_task)
                                yield AgentCancelled()
                                return
                    else:
                        outcomes = await asyncio.gather(
                            *(self._execute_tool_async(session, tool_call) for tool_call in batch)
                        )

                    for outcome in outcomes:
                        tool_call = outcome.tool_call
                        result_text = outcome.result_text
                        is_error = outcome.is_error

                        # ---- 循环检测 ----
                        consecutive_count = self._loop_detector.record(
                            tool_call.name,
                            tool_call.arguments,
                        )
                        is_stuck = self._loop_detector.is_stuck()

                        warning = self._build_loop_warning(
                            tool_call.name,
                            consecutive_count,
                            is_stuck,
                        )
                        if warning:
                            result_text += warning

                        if is_error:
                            total_tool_errors += 1
                            consecutive_failures += 1
                            if tool_call.source == "text_fallback" and tool_call.parse_error:
                                malformed_text_tool_retries += 1
                                if malformed_text_tool_retries >= MAX_MALFORMED_TEXT_TOOL_RETRIES:
                                    stop_for_malformed_text_tools = True
                                    result_text += (
                                        "\n\nCoomi stopped retrying malformed text tool calls "
                                        f"after {malformed_text_tool_retries} consecutive attempts. "
                                        "Please continue with a normal response or use a valid native tool call."
                                    )

                            # 连续失败上限提示必须写进同一个 tool result，
                            # 不能追加第二个同 ID 的 tool result。
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                result_text += (
                                    f"\n\n⚠️ 已连续 {consecutive_failures} 次工具调用失败。"
                                    f"请认真分析错误原因，尝试完全不同的方法。"
                                )
                                consecutive_failures = 0
                        else:
                            # 工具成功 → 重置连续失败计数
                            consecutive_failures = 0
                            malformed_text_tool_retries = 0

                        if _is_low_information_tool_result(result_text):
                            consecutive_low_info_results += 1
                            if consecutive_low_info_results >= MAX_CONSECUTIVE_LOW_INFO_RESULTS:
                                stop_for_no_progress = True
                                result_text += (
                                    "\n\nCoomi stopped this run after repeated low-information "
                                    f"tool results ({consecutive_low_info_results} in a row). "
                                    "Please summarize what was learned, ask the user for direction, "
                                    "or switch to a materially different approach."
                                )
                        else:
                            consecutive_low_info_results = 0

                        if consecutive_count >= LOOP_FORCE_BREAK_THRESHOLD:
                            force_break = True
                            break_msg = self._build_force_break_message(
                                tool_call.name,
                                consecutive_count,
                            )
                            result_text += f"\n\n{break_msg}"

                        yield ToolDone(
                            tool_name=tool_call.name,
                            elapsed=outcome.elapsed,
                            result_preview=result_text[:500] if result_text else None,
                            is_error=is_error,
                        )
                        add_tool_result(session, tool_call.id, result_text)
                        if stop_for_malformed_text_tools or stop_for_no_progress or force_break:
                            break

                    if stop_for_malformed_text_tools or stop_for_no_progress or force_break:
                        break

                # 工具执行后取消检查
                if self._cancel_token.is_cancelled:
                    yield AgentCancelled()
                    return

                # ---- 强制中断处理 ----
                if stop_for_malformed_text_tools:
                    yield AgentError(
                        message=(
                            "Stopped malformed text tool-call recovery after repeated invalid "
                            f"formats ({malformed_text_tool_retries} attempts)."
                        ),
                        is_fatal=False,
                    )
                    return

                if stop_for_no_progress:
                    yield AgentError(
                        message=(
                            "Stopped repeated low-information tool results before hitting "
                            f"MAX_ITERATIONS ({consecutive_low_info_results} consecutive results)."
                        ),
                        is_fatal=False,
                    )
                    return

                if force_break:
                    yield AgentError(
                        message=(
                            "Stopped repeated tool call loop before hitting MAX_ITERATIONS. "
                            "The session was saved; continue with a different approach."
                        ),
                        is_fatal=False,
                    )
                    return

                compress_event = await self._check_compress(session)
                if compress_event:
                    yield compress_event

                continue
            else:
                add_assistant_message(
                    session,
                    full_content if full_content.strip() else "",
                    reasoning_content=full_reasoning or None,
                )
                return

        # ---- 达到有效迭代上限 ----
        # P0-B: 不 yield AgentError 终结对话，而是注入一条提示消息让用户可继续
        source_file = os.path.abspath(__file__)
        summary = (
            f"⚠️ Agent 已达到最大迭代次数上限 (MAX_ITERATIONS={MAX_ITERATIONS})。\n\n"
            f"执行统计：\n"
            f"  - 有效迭代: {effective_iteration}\n"
            f"  - 工具调用: {total_tool_calls} (其中错误: {total_tool_errors})\n"
            f"  - 最后用户输入: {user_input[:200] if user_input else '(空)'}\n"
            f"  - 消息数: {len(session.messages)}\n\n"
            f"这通常意味着工具调用陷入了循环。你可以：\n"
            f"1. 输入新的指令让我继续\n"
            f"2. 检查上方的工具调用历史，找出循环原因\n"
            f"3. 用 /clear 清空会话重新开始"
        )
        # 注入为 assistant 消息（而非 AgentError），让对话可以继续
        add_assistant_message(session, summary, reasoning_content=None)
        # 同时 yield AgentError 作为通知（UI 可以显示为警告而非致命错误）
        yield AgentError(
            message=(
                f"达到最大迭代次数上限 (MAX_ITERATIONS={MAX_ITERATIONS})\n"
                f"  来源文件: {source_file}\n"
                f"  会话ID: {session.id}\n"
                f"  有效迭代: {effective_iteration}\n"
                f"  工具调用: {total_tool_calls} (错误: {total_tool_errors})\n"
                f"  消息数: {len(session.messages)}\n"
                f"注意: 会话状态已保存，你可以继续输入来恢复工作。"
            ),
            is_fatal=False,  # 迭代上限，用户可继续对话
        )
