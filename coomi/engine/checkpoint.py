"""Checkpoint 管理器 — 持久化 Loop 进度"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Checkpoint, LoopSession, Spec


def create_loop_dir(cwd: str, loop_id: str | None = None) -> tuple[Path, str]:
    """创建 Loop 工作目录
    
    在项目根目录下创建 .coomi/loops/{loop_id}/
    
    Returns:
        (loop_dir, loop_id)
    """
    if loop_id is None:
        loop_id = uuid.uuid4().hex[:8]
    
    loop_dir = Path(cwd) / ".coomi" / "loops" / loop_id
    loop_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_dir = loop_dir / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    
    return loop_dir, loop_id


def save_spec_copy(loop_dir: Path, spec: "Spec") -> None:
    """保存 spec 副本到 loop 目录"""
    spec_path = loop_dir / "spec.md"
    content = _spec_to_markdown(spec)
    spec_path.write_text(content, encoding="utf-8")


def save_state(loop_session: "LoopSession") -> None:
    """保存 LoopSession 状态到 state.json"""
    if loop_session.loop_dir is None:
        return
    
    state = {
        "loop_id": loop_session.loop_id,
        "spec": {
            "title": loop_session.spec.title,
            "goal": loop_session.spec.goal,
            "steps": loop_session.spec.steps,
            "constraints": loop_session.spec.constraints,
            "acceptance_criteria": loop_session.spec.acceptance_criteria,
            "resources": loop_session.spec.resources,
            "tools_allowed": loop_session.spec.tools_allowed,
            "tools_forbidden": loop_session.spec.tools_forbidden,
        },
        "status": loop_session.status.value,
        "current_step": loop_session.current_step,
        "retry_counts": loop_session.retry_counts,
        "started_at": loop_session.started_at.isoformat(),
        "last_active_at": loop_session.last_active_at.isoformat(),
    }
    
    state_path = loop_session.loop_dir / "state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_checkpoint(loop_dir: Path, checkpoint: "Checkpoint") -> None:
    """保存单个 checkpoint"""
    checkpoints_dir = loop_dir / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    
    cp_data = {
        "step_index": checkpoint.step_index,
        "step_summary": checkpoint.step_summary,
        "files_changed": checkpoint.files_changed,
        "created_at": checkpoint.created_at.isoformat(),
    }
    
    cp_path = checkpoints_dir / f"step_{checkpoint.step_index}.json"
    cp_path.write_text(json.dumps(cp_data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_issue(loop_dir: Path, step_index: int, step_description: str, error: str) -> None:
    """向 ISSUE.md 追加一条问题记录"""
    issue_path = loop_dir / "ISSUE.md"
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"## Step {step_index + 1} - {timestamp}\n\n"
        f"**步骤描述:** {step_description}\n\n"
        f"**错误信息:**\n```\n{error}\n```\n\n"
        f"**状态:** 需要人工介入\n\n---\n\n"
    )
    
    if issue_path.exists():
        current = issue_path.read_text(encoding="utf-8")
        entry = current + entry
    
    issue_path.write_text(entry, encoding="utf-8")


def load_state(loop_dir: Path) -> dict | None:
    """从 state.json 加载状态"""
    state_path = loop_dir / "state.json"
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _spec_to_markdown(spec: "Spec") -> str:
    """将 Spec 对象转换为 Markdown"""
    lines = [f"# {spec.title}", "", "## 目标", spec.goal, ""]
    
    if spec.steps:
        lines.append("## 步骤")
        for i, step in enumerate(spec.steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")
    
    if spec.constraints:
        lines.append("## 约束")
        for c in spec.constraints:
            lines.append(f"- {c}")
        lines.append("")
    
    if spec.acceptance_criteria:
        lines.append("## 验收标准")
        for ac in spec.acceptance_criteria:
            lines.append(f"- {ac}")
        lines.append("")
    
    if spec.resources:
        lines.append("## 可用资源")
        for k, v in spec.resources.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    
    if spec.tools_allowed or spec.tools_forbidden:
        lines.append("## 可用工具")
        for t in spec.tools_allowed:
            lines.append(f"- {t}")
        for t in spec.tools_forbidden:
            lines.append(f"- 禁止执行: {t}")
        lines.append("")
    
    return "\n".join(lines)
