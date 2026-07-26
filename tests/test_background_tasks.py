from __future__ import annotations

import asyncio

import pytest

from coomi.engine.background_tasks import BackgroundResult, BackgroundTaskRegistry
from coomi.engine.process_registry import ProcessRegistry
from coomi.engine.tool_executor import ToolExecutionOutcome
from coomi.types import ToolCall


def _make_outcome(text: str = "done", is_error: bool = False) -> ToolExecutionOutcome:
    return ToolExecutionOutcome(
        tool_call=ToolCall(id="call_1", name="PowerShell", arguments={}),
        result_text=text,
        is_error=is_error,
        elapsed=0.0,
    )


@pytest.mark.asyncio
async def test_detach_limits_to_single_slot():
    reg = BackgroundTaskRegistry()
    assert reg.can_detach()

    forever: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.ensure_future(forever)
    reg.detach("call_1", "PowerShell", task)

    assert reg.has_active()
    assert not reg.can_detach()

    task.cancel()
    # 让 watcher 处理取消并回灌
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_completed_result_is_drained_and_reinjected():
    reg = BackgroundTaskRegistry()

    async def quick() -> ToolExecutionOutcome:
        return _make_outcome("hello from background")

    task = asyncio.ensure_future(quick())
    task_id = reg.detach("call_1", "PowerShell", task)

    result = await reg.wait_any_completed()
    assert isinstance(result, BackgroundResult)
    assert result.task_id == task_id
    assert result.tool_call_id == "call_1"
    assert "hello from background" in result.result_text
    assert not result.is_error

    # 放回后应可被 drain 取出（模拟主循环回灌路径）
    reg.requeue_completed(result)
    drained = reg.drain_completed()
    assert len(drained) == 1
    assert drained[0].result_text == result.result_text
    # 结果被移出后不再有待处理工作
    assert not reg.has_pending_work()


@pytest.mark.asyncio
async def test_active_task_keeps_pending_work_until_result_queued():
    reg = BackgroundTaskRegistry()
    gate: asyncio.Future = asyncio.get_event_loop().create_future()

    async def blocked() -> ToolExecutionOutcome:
        await gate
        return _make_outcome("late")

    task = asyncio.ensure_future(blocked())
    reg.detach("call_1", "PowerShell", task)

    # 任务尚未完成 → 仍有待处理工作
    assert reg.has_pending_work()

    gate.set_result(None)
    result = await reg.wait_any_completed()
    assert result.result_text == "late"
    # active 已清空且结果已取出
    assert not reg.has_pending_work()


@pytest.mark.asyncio
async def test_cancel_all_reinjects_stopped_notice():
    reg = BackgroundTaskRegistry()
    forever: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.ensure_future(forever)
    task_id = reg.detach("call_1", "PowerShell", task)

    reg.cancel_all()
    result = await reg.wait_any_completed()
    assert result.task_id == task_id
    assert result.is_error
    assert "停止" in result.result_text


def test_process_registry_register_unregister():
    reg = ProcessRegistry()

    class _FakeProc:
        def __init__(self):
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._alive = False

    proc = _FakeProc()
    reg.register(proc)
    assert reg.has_active()

    killed = reg.kill_all()
    assert killed == 1
    assert not reg.has_active()

    # unregister 后不再计入
    reg.unregister(proc)
    assert not reg.has_active()
