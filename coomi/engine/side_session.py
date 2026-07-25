"""并发只读交流执行器（side session）

用途：主任务的某个工具/PowerShell 正在阻塞执行时（`await asyncio.to_thread`
期间事件循环空闲、LLM 资源闲置），在一条**独立、只读**的旁路会话上跑用户的
临时交流内容或「立即引导」内容，充分利用被工具阻塞窗口内的模型资源。

关键设计约束：
  - **只读**：side session 的 AgentLoop 强制 plan_mode(read_only)，Edit/Write/
    PowerShell/Bash 等写/执行工具全部被拦，因此与主任务正在执行的工具**零冲突**。
  - **不污染主线**：跑在主 session 的快照克隆上，结束即弃，不回写主 session。
  - **不碰主 UI 流**：回复只写进交流窗口，绝不触碰主 log / StreamingPreview。
  - **仅限本次**：交流结束后主任务照常从工具返回处接续，不受任何影响。
"""
from __future__ import annotations

import uuid
from typing import Any

from ..types import Session
from ..ui.events import (
    AgentCancelled,
    AgentError,
    ReasoningChunk,
    TextChunk,
    ToolDone,
    ToolStart,
)
from .loop import AgentLoop

SIDE_READONLY_HINT = (
    "\n\n[临时只读交流] 你正处在一段临时的旁路对话中：主任务的一个工具正在后台执行，"
    "用户利用这段空档与你临时交流。你此刻**只有只读能力**（可以读文件、检索、查资料），"
    "不能改写文件、不能执行命令。请基于已有上下文简洁作答；若需要改动或执行，"
    "说明思路即可，实际操作留待主任务恢复后进行。这段交流仅限本次，不影响主任务。"
)


def _clone_session(source: Session) -> Session:
    """把主 session 克隆成一条独立的旁路会话快照。

    messages 做浅拷贝（复制列表，元素本身只读不改），system_prompt 追加只读提示。
    不复制 history_path —— 旁路会话绝不落盘，结束即弃。
    """
    return Session(
        id=f"side-{uuid.uuid4().hex[:8]}",
        system_prompt=source.system_prompt + SIDE_READONLY_HINT,
        messages=list(source.messages),
        current_model=source.current_model,
        active_skills=list(source.active_skills),
        selected_mcps=list(source.selected_mcps),
        history_path=None,
    )


async def run_side_conversation(
    comm_panel: Any,
    source_session: Session,
    provider: Any,
    tool_registry: Any,
    context_window_size: int,
    text: str,
    *,
    app_context: Any = None,
    permission_system: Any = None,
    hook_system: Any = None,
    project_path: str | None = None,
) -> None:
    """在独立只读 side session 上执行一次交流，回复写入交流窗口。

    该协程设计为用 `asyncio.create_task` 起为普通任务（非 exclusive worker），
    因此不会杀死正在跑的主 worker。异常在内部吞掉并显示到交流窗，绝不冒泡影响主任务。
    """
    clone = _clone_session(source_session)
    side_agent = AgentLoop(
        provider,
        tool_registry,
        context_window_size,
        app_context=app_context,
        permission_system=permission_system,
        hook_system=hook_system,
        project_path=project_path,
    )
    # 强制只读：借用 plan_mode 打开 tool_executor.read_only_mode，
    # 写/执行工具会被拦下，与主任务的工具执行零冲突。
    side_agent.set_plan_mode(True)

    reply = ""

    try:
        comm_panel.set_busy(True)
        comm_panel.begin_reply(text)

        async for event in side_agent.run_stream(clone, text):
            if isinstance(event, TextChunk):
                reply += event.content
                comm_panel.show_reply_streaming(reply)
            elif isinstance(event, ReasoningChunk):
                # 推理内容不并入正式回复，仅作生成中状态提示
                comm_panel.set_thinking()
            elif isinstance(event, ToolStart):
                comm_panel.set_tool_status(event.tool_name)
            elif isinstance(event, ToolDone):
                comm_panel.set_thinking()
            elif isinstance(event, AgentCancelled):
                break
            elif isinstance(event, AgentError):
                comm_panel.append_reply(
                    reply, error=f"交流出错：{event.message}"
                )
                return

        comm_panel.append_reply(reply)
    except Exception as e:  # noqa: BLE001 — 旁路会话绝不能把异常冒泡到主任务
        comm_panel.append_reply(reply, error=f"交流异常：{e}")
    finally:
        comm_panel.set_busy(False)
