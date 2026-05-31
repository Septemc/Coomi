"""LoopRunner — Loop 模式主运行器

核心流程:
1. 感知: 读当前状态（checkpoint/step）
2. 决策: 构造 prompt 让 LLM 选择下一步操作
3. 执行: 调用工具，捕获结果
4. 评估: 判断当前步骤是否完成
5. 持久化: 保存 checkpoint
6. 检查: 是否全部完成？
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import AsyncIterator, Callable, Any

from ..types import (
    Checkpoint,
    LoopSession,
    LoopStatus,
    Spec,
    StepResult,
)
from ..ui.events import (
    AgentCancelled,
    AgentError,
    AgentEvent,
    LoopProgress,
    LoopStepStart,
    LoopStepDone,
    LoopIssueCreated,
    TextChunk,
)
from .checkpoint import (
    append_issue,
    create_loop_dir,
    save_checkpoint,
    save_spec_copy,
    save_state,
)
from .retry_policy import RetryAction, RetryPolicy
from .spec_parser import parse_spec_file
from .loop import AgentLoop, CancelToken
from .session import Session, build_system_prompt
from ..services.llm.provider import LLMProvider
from ..services.memory.manager import MemoryManager
from ..services.memory.recall import MemoryRecall
from ..tools.registry import ToolRegistry


LOOP_SYSTEM_PROMPT_EXTRA = """
## Loop Mode

You are operating in **LOOP MODE** — an autonomous long-running task execution mode. 
Your goal is to execute ALL steps in the spec document until completion.

### Rules
1. Execute steps in order. Only proceed to the next step after the current one is confirmed complete.
2. If a step fails, diagnose the error and try again. If the same method fails 3 times, try a DIFFERENT approach.
3. After each completed step, output a clear summary: "✅ Step N complete: <summary>"
4. If a step fails 6 times total with all approaches, output "⚠️ Step N skipped: <reason>" and continue to the next step.
5. After ALL steps are done, verify against acceptance criteria and output "✅ LOOP COMPLETE".
6. Do NOT ask the user for confirmation — just execute autonomously.
7. Keep responses concise — focus on progress and results.
"""


class LoopRunner:
    """Loop 模式主运行器"""

    MAX_LOOP_ITERATIONS = 100  # 全局迭代上限，防止死循环

    def __init__(
        self,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        context_window_size: int = 256_000,
        app_context: Any = None,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_window_size = context_window_size
        self.app_context = app_context
        self._cancel_token = CancelToken()
        self._agent: AgentLoop | None = None
        self._step_result: StepResult = StepResult.FAILED  # _execute_step 的结果容器

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel_token

    async def start_loop(
        self,
        cwd: str,
        spec_path: str | None = None,
        spec: Spec | None = None,
        loop_id: str | None = None,
        memory_manager: MemoryManager | None = None,
        memory_recall: MemoryRecall | None = None,
        display_name: str = "",
        on_state_change: Callable[[LoopSession], None] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """启动 Loop 模式的主入口
        
        Args:
            cwd: 当前工作目录
            spec_path: spec 文件路径（与 spec 二选一）
            spec: 已解析的 Spec 对象（与 spec_path 二选一）
            loop_id: 手动指定 loop_id（用于恢复）
            memory_manager: 记忆管理器
            memory_recall: 记忆召回器
            display_name: 模型显示名
            on_state_change: 状态变化回调
        """
        # 1. 加载/解析 spec
        if spec is None and spec_path:
            spec = parse_spec_file(spec_path)
        if spec is None:
            yield AgentError(message="No spec provided for loop mode", is_fatal=True)
            return

        # 2. 创建 loop 目录
        loop_dir, loop_id = create_loop_dir(cwd, loop_id)
        save_spec_copy(loop_dir, spec)

        # 3. 创建 LoopSession
        loop_session = LoopSession(
            loop_id=loop_id,
            spec=spec,
            loop_dir=loop_dir,
        )

        # 4. 构建 System Prompt
        system_prompt = await build_system_prompt(
            memory_manager=memory_manager,
            memory_recall=memory_recall,
            current_context=_build_spec_context(spec),
            cwd=cwd,
            model_display=display_name,
        )
        system_prompt += LOOP_SYSTEM_PROMPT_EXTRA

        # 5. 创建 AgentLoop
        self._agent = AgentLoop(
            self.llm,
            self.tool_registry,
            self.context_window_size,
            app_context=self.app_context,
        )

        # 6. 开始执行循环
        save_state(loop_session)
        if on_state_change:
            on_state_change(loop_session)

        retry_policy = RetryPolicy()
        session = Session(
            id=f"loop_{loop_id}",
            system_prompt=system_prompt,
        )

        global_iteration = 0

        while loop_session.current_step < len(spec.steps):
            step_index = loop_session.current_step
            step_desc = spec.steps[step_index]

            global_iteration += 1
            if global_iteration > self.MAX_LOOP_ITERATIONS:
                source_file = os.path.abspath(__file__)
                yield AgentError(
                    message=(
                        f"Loop 超过最大迭代次数上限 (MAX_LOOP_ITERATIONS={self.MAX_LOOP_ITERATIONS})\n"
                        f"  来源文件: {source_file}\n"
                        f"  Loop ID: {loop_session.loop_id}\n"
                        f"  任务: {spec.title}\n"
                        f"  当前步骤: {step_index + 1}/{len(spec.steps)} — {step_desc}\n"
                        f"  全局迭代: {global_iteration}\n"
                        f"  Loop 目录: {loop_dir}\n"
                        f"提示: Loop 在 {self.MAX_LOOP_ITERATIONS} 次全局迭代后仍未完成所有步骤，"
                        f"可能是某个步骤陷入了无限重试。请检查步骤内容或增加 MAX_LOOP_ITERATIONS 上限。"
                    ),
                    is_fatal=True,  # Loop 迭代上限是致命的 — 任务确实无法完成
                )
                loop_session.status = LoopStatus.FAILED
                save_state(loop_session)
                return

            if self._cancel_token.is_cancelled:
                loop_session.status = LoopStatus.CANCELLED
                save_state(loop_session)
                yield AgentCancelled()
                return

            yield LoopStepStart(
                step_index=step_index,
                step_description=step_desc,
                total_steps=len(spec.steps),
            )
            yield LoopProgress(
                current_step=step_index + 1,
                total_steps=len(spec.steps),
                status=loop_session.status,
            )

            # 构建步骤执行 prompt
            step_prompt = _build_step_prompt(spec, step_index, loop_session)
            async for event in self._execute_step(
                session, step_prompt, spec, step_index, loop_session, retry_policy
            ):
                yield event

            result = self._step_result

            if result == StepResult.SUCCESS:
                checkpoint = Checkpoint(
                    step_index=step_index,
                    step_summary=f"Step {step_index + 1}: {step_desc} — completed",
                    files_changed=[],
                )
                loop_session.checkpoints.append(checkpoint)
                save_checkpoint(loop_dir, checkpoint)
                loop_session.current_step += 1
                loop_session.retry_counts.pop(step_index, None)
                retry_policy.reset_step(step_index)
                yield LoopStepDone(step_index=step_index, success=True)

            elif result == StepResult.SKIP:
                # 写入 ISSUE.md
                ctx = retry_policy.get_context(step_index)
                append_issue(loop_dir, step_index, step_desc, ctx.last_error)
                yield LoopIssueCreated(step_index=step_index, step_description=step_desc)
                loop_session.current_step += 1
                retry_policy.reset_step(step_index)
                yield LoopStepDone(step_index=step_index, success=False)

            elif result == StepResult.FAILED:
                source_file = os.path.abspath(__file__)
                loop_session.status = LoopStatus.FAILED
                save_state(loop_session)
                yield AgentError(
                    message=(
                        f"Loop 执行失败，步骤 {step_index + 1}/{len(spec.steps)}: {step_desc}\n"
                        f"  来源文件: {source_file}\n"
                        f"  Loop ID: {loop_session.loop_id}\n"
                        f"  任务: {spec.title}\n"
                        f"  Loop 目录: {loop_dir}\n"
                        f"  全局迭代: {global_iteration}"
                    ),
                    is_fatal=True,  # 步骤执行失败是致命的
                )
                return

            elif result == StepResult.RETRY:
                # 继续重试当前步骤
                pass

            loop_session.last_active_at = datetime.now()
            save_state(loop_session)
            if on_state_change:
                on_state_change(loop_session)

        # 全部完成
        loop_session.status = LoopStatus.COMPLETED
        save_state(loop_session)
        if on_state_change:
            on_state_change(loop_session)

    async def _execute_step(
        self,
        session: Session,
        step_prompt: str,
        spec: Spec,
        step_index: int,
        loop_session: LoopSession,
        retry_policy: RetryPolicy,
    ) -> AsyncIterator[AgentEvent]:
        """执行单个步骤，包含重试逻辑

        结果通过 self._step_result 返回（async generator 不能 return value）。
        调用方在消费完 async for 后读取 self._step_result。
        """
        retry_ctx = retry_policy.get_context(step_index)

        while True:
            if self._cancel_token.is_cancelled:
                self._step_result = StepResult.FAILED
                return

            error_occurred = False
            last_error = ""

            try:
                # 运行 AgentLoop 执行当前步骤
                async for event in self._agent.run_stream(session, step_prompt):
                    yield event

                    if isinstance(event, AgentError):
                        if event.is_fatal:
                            # 致命错误（步骤执行失败）→ 触发重试逻辑
                            error_occurred = True
                            last_error = event.message
                        else:
                            # 非致命错误（LLM 降级、迭代上限等）→ 透传，不触发重试
                            pass

                    elif isinstance(event, AgentCancelled):
                        self._step_result = StepResult.FAILED
                        return

            except Exception as e:
                error_occurred = True
                last_error = str(e)

            if not error_occurred:
                # 成功 — 检查 Agent 是否确认步骤完成
                self._step_result = StepResult.SUCCESS
                return

            # 失败 — 决定下一步
            action, delay = retry_policy.decide_action(step_index, last_error)

            if action == RetryAction.RETRY_SAME:
                # 等待后重试
                yield TextChunk(content=f"\n[dim]Step {step_index + 1} failed, retrying in {delay:.0f}s... ({last_error[:100]})[/dim]\n")
                await retry_policy.wait(delay)
                loop_session.retry_counts[step_index] = retry_ctx.attempt
                continue

            elif action == RetryAction.RETRY_DIFFERENT:
                # 换方法重试 — 让 LLM 生成替代方案
                yield TextChunk(content=f"\n[yellow]Step {step_index + 1} failed {retry_ctx.attempt} times, trying different approach...[/yellow]\n")
                step_prompt = _build_alternative_prompt(spec, step_index, last_error)
                await retry_policy.wait(delay)
                loop_session.retry_counts[step_index] = retry_ctx.attempt
                continue

            elif action == RetryAction.SKIP:
                yield TextChunk(content=f"\n[red]Step {step_index + 1} failed after {retry_ctx.attempt} attempts. Skipping...[/red]\n")
                self._step_result = StepResult.SKIP
                return

            else:
                self._step_result = StepResult.FAILED
                return


def _build_spec_context(spec: Spec) -> str:
    """构建 spec 上下文文本，注入 System Prompt"""
    lines = [
        f"## Loop Task: {spec.title}",
        f"**Goal:** {spec.goal}",
        f"**Total Steps:** {len(spec.steps)}",
        "",
    ]
    if spec.constraints:
        lines.append("**Constraints:**")
        for c in spec.constraints:
            lines.append(f"- {c}")
        lines.append("")
    if spec.acceptance_criteria:
        lines.append("**Acceptance Criteria:**")
        for ac in spec.acceptance_criteria:
            lines.append(f"- {ac}")
        lines.append("")
    return "\n".join(lines)


def _build_step_prompt(spec: Spec, step_index: int, loop_session: LoopSession) -> str:
    """为当前步骤构建执行 prompt"""
    total = len(spec.steps)
    step_desc = spec.steps[step_index]
    progress = f"Step {step_index + 1}/{total}"

    # 如果之前已有完成的步骤，提供简要上下文
    completed_context = ""
    if loop_session.checkpoints:
        completed_context = "\n\nPreviously completed steps:\n" + "\n".join(
            f"- {cp.step_summary}" for cp in loop_session.checkpoints
        )

    return (
        f"Execute {progress} of the loop task:\n\n"
        f"**Task:** {spec.title}\n"
        f"**Goal:** {spec.goal}\n"
        f"**Current Step ({progress}):** {step_desc}{completed_context}\n\n"
        f"Execute this step now. If it succeeds, clearly state '✅ Step {step_index + 1} complete'."
        f" If it fails, explain the error so I can retry with a different approach."
    )


def _build_alternative_prompt(spec: Spec, step_index: int, last_error: str) -> str:
    """构建替代方案 prompt"""
    step_desc = spec.steps[step_index]
    return (
        f"The previous approach for this step failed. Try a DIFFERENT method.\n\n"
        f"**Step {step_index + 1}:** {step_desc}\n"
        f"**Previous error:** {last_error[:500]}\n\n"
        f"Analyze the error and try an alternative approach. "
        f"If the new approach succeeds, state '✅ Step {step_index + 1} complete'. "
        f"If it also fails, explain why so the next alternative can be tried."
    )
