"""后台任务注册表 — 支撑「插队 detach」机制。

场景：主任务正卡在某个阻塞工具（如 PowerShell 长命令）上，用户插队。
此时不强杀子进程，而是把「等待该工具完成」的 asyncio 任务转入后台，
主流立即接续处理插队内容；工具真正结束后，其结果被回灌为一条新消息，
Agent 自动续跑一轮，让 LLM 看到迟到的结果并继续。

约束（本期）：同一时刻至多 1 个后台任务。

与 ProcessRegistry 的分工：
- 本表理解「任务语义」：编号、工具名、参数、完成结果、回灌。
- ProcessRegistry 只管操作系统进程句柄，供「停止真杀」使用。
  detach 不碰它——被转后台的子进程仍在正常运行，直到自然结束。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tool_executor import ToolExecutionOutcome


@dataclass
class BackgroundTask:
    """一个被转入后台的工具执行。"""

    task_id: int
    tool_name: str
    task: "asyncio.Task[ToolExecutionOutcome]"


@dataclass
class BackgroundResult:
    """后台任务完成后的结果，等待回灌。"""

    task_id: int
    tool_name: str
    tool_call_id: str
    result_text: str
    is_error: bool


class BackgroundTaskRegistry:
    """管理被 detach 的工具任务，收集其完成结果供主循环回灌。"""

    MAX_ACTIVE = 1

    def __init__(self) -> None:
        self._next_id = 1
        self._active: dict[int, BackgroundTask] = {}
        self._completed: asyncio.Queue[BackgroundResult] = asyncio.Queue()
        self._watchers: set[asyncio.Task] = set()

    def can_detach(self) -> bool:
        """是否还能再转一个后台任务（受 MAX_ACTIVE 限制）。"""
        return len(self._active) < self.MAX_ACTIVE

    def has_active(self) -> bool:
        return bool(self._active)

    def active_count(self) -> int:
        return len(self._active)

    def has_pending_work(self) -> bool:
        """仍有后台任务在跑，或有已完成但尚未回灌的结果。

        供 Agent 空闲判定：只要为真就应等待/续跑，避免结果被遗留到下次输入。
        """
        return bool(self._active) or not self._completed.empty()

    def detach(
        self,
        tool_call_id: str,
        tool_name: str,
        task: "asyncio.Task[ToolExecutionOutcome]",
    ) -> int:
        """把一个正在运行的工具任务转入后台，返回分配的编号。

        启动一个 watcher 协程 await 该任务；完成后把结果塞进 `_completed`，
        供主循环在下一轮开头 drain 出来回灌。
        """
        task_id = self._next_id
        self._next_id += 1
        self._active[task_id] = BackgroundTask(task_id, tool_name, task)

        watcher = asyncio.ensure_future(self._watch(task_id, tool_call_id, tool_name, task))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return task_id

    async def _watch(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        task: "asyncio.Task[ToolExecutionOutcome]",
    ) -> None:
        result_text: str
        is_error: bool
        try:
            outcome = await task
            result_text = outcome.result_text
            is_error = outcome.is_error
        except asyncio.CancelledError:
            # 任务被「停止真杀」取消：仍回灌一条说明，让 LLM 知道结局。
            result_text = f"[后台任务 #{task_id}] 已被用户停止（子进程被强制终止）。"
            is_error = True
        except Exception as exc:  # 工具线程内的异常兜底
            result_text = (
                f"[后台任务 #{task_id}] 执行时崩溃：{type(exc).__name__}: {exc}"
            )
            is_error = True

        # 先入队再移出 active，避免出现「active 已空、结果尚未入队」的空窗，
        # 否则等待方可能误判无后台任务而提前结束（丢失唤醒）。
        await self._completed.put(
            BackgroundResult(
                task_id=task_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result_text=result_text,
                is_error=is_error,
            )
        )
        self._active.pop(task_id, None)

    def drain_completed(self) -> list[BackgroundResult]:
        """非阻塞取出所有已完成结果（供主循环每轮开头回灌）。"""
        results: list[BackgroundResult] = []
        while True:
            try:
                results.append(self._completed.get_nowait())
            except asyncio.QueueEmpty:
                break
        return results

    async def wait_any_completed(self) -> BackgroundResult:
        """阻塞等待至少一个后台任务完成（供 Agent 空闲时自动续跑）。

        取出后若调用方并不打算立即消费，应用 `requeue_completed` 放回，
        以便主循环下一轮开头的 `drain_completed` 统一回灌。
        """
        return await self._completed.get()

    def requeue_completed(self, result: BackgroundResult) -> None:
        """把 `wait_any_completed` 取出的结果放回队列，交给主循环 drain 回灌。"""
        self._completed.put_nowait(result)

    def cancel_all(self) -> None:
        """取消所有活动后台任务（供「停止真杀」；实际子进程由 ProcessRegistry 杀）。

        watcher 会捕获 CancelledError 并回灌一条「已停止」说明，故此处
        不清空 `_completed`——那些说明仍会在下一轮被 drain 出来。
        """
        for bg in list(self._active.values()):
            if not bg.task.done():
                bg.task.cancel()
