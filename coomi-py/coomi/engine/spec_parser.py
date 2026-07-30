"""Spec 文档解析器 — Markdown → 结构化 Spec 对象"""
from __future__ import annotations

import re
from pathlib import Path

from ..types import Spec


def parse_spec_file(spec_path: str | Path) -> Spec:
    """解析 spec Markdown 文件，返回结构化的 Spec 对象
    
    支持的 Markdown 结构：
        # 任务标题
        ## 目标
        ## 步骤
        ## 约束
        ## 验收标准
        ## 可用资源
        ## 可用工具
    """
    path = Path(spec_path)
    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    content = path.read_text(encoding="utf-8")

    title = _extract_section(content, r"^#\s+(.+)", default=path.stem)
    goal = _extract_section(content, r"##\s*目标\s*\n(.*?)(?=\n##|\Z)")
    steps = _extract_list(content, r"##\s*步骤\s*\n")
    constraints = _extract_list(content, r"##\s*约束\s*\n")
    acceptance_criteria = _extract_list(content, r"##\s*验收标准\s*\n")
    resources = _extract_kv(content, r"##\s*可用资源\s*\n")

    tools_allowed: list[str] = []
    tools_forbidden: list[str] = []
    tools_section = _extract_section(content, r"##\s*可用工具\s*\n(.*?)(?=\n##|\Z)")
    if tools_section:
        for line in tools_section.strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line.lower().startswith("禁止") or line.lower().startswith("forbidden"):
                forbidden = re.sub(r"^禁止[执行:：\s]*", "", line, flags=re.IGNORECASE)
                forbidden = re.sub(r"^forbidden[:：\s]*", "", forbidden, flags=re.IGNORECASE)
                tools_forbidden.append(forbidden)
            else:
                tools_allowed.append(line)

    return Spec(
        title=title,
        goal=goal,
        steps=steps,
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        resources=resources,
        tools_allowed=tools_allowed,
        tools_forbidden=tools_forbidden,
    )


def _extract_section(content: str, pattern: str, default: str = "") -> str:
    """从 Markdown 中提取某个 section 的文本内容"""
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return default
    return m.group(1).strip() if m.lastindex else m.group(0).strip()


def _extract_list(content: str, header_pattern: str) -> list[str]:
    """从 Markdown section 中提取列表项（- 或 1. 开头）"""
    section = _extract_section(content, header_pattern + r"(.*?)(?=\n##|\Z)")
    if not section:
        return []
    items: list[str] = []
    for line in section.strip().split("\n"):
        stripped = line.strip()
        # 匹配 "- item" 或 "1. item"
        m = re.match(r"^(?:-|\d+\.)\s+(.+)", stripped)
        if m:
            items.append(m.group(1).strip())
        elif stripped:
            items.append(stripped)
    return items


def _extract_kv(content: str, header_pattern: str) -> dict[str, str]:
    """从 Markdown section 中提取 key: value 对"""
    section = _extract_section(content, header_pattern + r"(.*?)(?=\n##|\Z)")
    if not section:
        return {}
    result: dict[str, str] = {}
    for line in section.strip().split("\n"):
        line = line.strip().lstrip("- ").strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result
