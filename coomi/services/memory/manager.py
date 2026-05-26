"""记忆管理器 - 管理全局和项目级记忆"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .types import Memory, MemoryType


# 陈旧度阈值（天）
STALENESS_THRESHOLD_DAYS = 7

# 记忆文件模板
MEMORY_INDEX_HEADER = """# Memory Index
> Auto-generated. Each entry is one line, under ~150 characters.

"""

MEMORY_TEMPLATE = """---
name: {name}
description: {description}
type: {memory_type}
created: {created}
updated: {updated}
---

{content}"""


class MemoryManager:
    """记忆管理器

    双层存储架构：
    1. .coomi/memory/ - 项目目录（最高优先级）
    2. ~/.coomi/projects/{project_hash}/memory/ - 项目级全局存储
    3. ~/.coomi/memory/ - 全局记忆（始终加载）
    """

    def __init__(self, project_path: str | None = None):
        """
        Args:
            project_path: 项目根目录路径（用于生成项目 hash）
        """
        self.global_dir = self._get_global_memory_dir()
        self.project_dir = self._get_project_memory_dir(project_path)

        # 确保目录存在
        self._ensure_dir(self.global_dir)
        self._ensure_dir(self.project_dir)

    def _get_global_memory_dir(self) -> Path:
        """获取全局记忆目录"""
        home = Path.home()
        return home / ".coomi" / "memory"

    def _get_project_memory_dir(self, project_path: str | None) -> Path:
        """获取项目记忆目录"""
        if not project_path:
            return self._get_global_memory_dir()

        project_path = Path(project_path).resolve()
        project_hash = self._generate_project_hash(project_path)
        return Path.home() / ".coomi" / "projects" / project_hash / "memory"

    def _generate_project_hash(self, project_path: Path) -> str:
        """生成项目唯一标识"""
        import hashlib
        path_str = str(project_path).encode()
        return hashlib.md5(path_str).hexdigest()[:12]

    def _ensure_dir(self, dir_path: Path) -> None:
        """确保目录存在"""
        dir_path.mkdir(parents=True, exist_ok=True)

    def _get_index_path(self, base_dir: Path) -> Path:
        """获取索引文件路径"""
        return base_dir / "MEMORY.md"

    def list_memories(self, memory_type: MemoryType | None = None) -> list[Memory]:
        """列出所有记忆

        Args:
            memory_type: 可选过滤类型

        Returns:
            list[Memory]: 记忆列表
        """
        memories = []

        # 扫描项目目录（优先级高）
        memories.extend(self._scan_dir(self.project_dir, memory_type))

        # 扫描全局目录（避免重复）
        project_names = {m.name for m in memories}
        for m in self._scan_dir(self.global_dir, memory_type):
            if m.name not in project_names:
                memories.append(m)

        return memories

    def _scan_dir(self, dir_path: Path, memory_type: MemoryType | None = None) -> list[Memory]:
        """扫描目录下的记忆文件"""
        memories = []
        if not dir_path.exists():
            return memories

        for filepath in dir_path.glob("*.md"):
            if filepath.name == "MEMORY.md":
                continue

            try:
                content = filepath.read_text(encoding="utf-8")
                memory = Memory.from_file(str(filepath), content)
                if memory:
                    # 检查陈旧度
                    if memory.memory_type in (MemoryType.PROJECT, MemoryType.REFERENCE):
                        age = datetime.now() - memory.updated_at
                        if age > timedelta(days=STALENESS_THRESHOLD_DAYS):
                            memory.is_stale = True

                    # 过滤类型
                    if memory_type is None or memory.memory_type == memory_type:
                        memories.append(memory)
            except Exception:
                continue

        return memories

    def get_memory(self, name: str) -> Memory | None:
        """获取指定记忆

        Args:
            name: 记忆名称

        Returns:
            Memory | None: 记忆对象
        """
        # 先查项目目录
        memory = self._load_from_dir(self.project_dir, name)
        if memory:
            return memory

        # 再查全局目录
        return self._load_from_dir(self.global_dir, name)

    def _load_from_dir(self, dir_path: Path, name: str) -> Memory | None:
        """从指定目录加载记忆"""
        filepath = dir_path / f"{name}.md"
        if not filepath.exists():
            return None

        try:
            content = filepath.read_text(encoding="utf-8")
            return Memory.from_file(str(filepath), content)
        except Exception:
            return None

    def save_memory(self, memory: Memory, to_global: bool = False) -> bool:
        """保存记忆

        Args:
            memory: 记忆对象
            to_global: 是否保存到全局目录

        Returns:
            bool: 是否成功
        """
        target_dir = self.global_dir if to_global else self.project_dir
        filepath = target_dir / f"{memory.name}.md"

        try:
            content = memory.to_frontmatter()
            filepath.write_text(content, encoding="utf-8")
            return True
        except Exception:
            return False

    def delete_memory(self, name: str) -> bool:
        """删除记忆

        Args:
            name: 记忆名称

        Returns:
            bool: 是否成功
        """
        # 优先删除项目目录
        project_path = self.project_dir / f"{name}.md"
        if project_path.exists():
            try:
                project_path.unlink()
                return True
            except Exception:
                return False

        # 删除全局目录
        global_path = self.global_dir / f"{name}.md"
        if global_path.exists():
            try:
                global_path.unlink()
                return True
            except Exception:
                return False

        return False

    def refresh_index(self) -> None:
        """刷新 MEMORY.md 索引文件"""
        memories = self.list_memories()

        lines = [MEMORY_INDEX_HEADER]
        for memory in memories:
            stale_marker = " [stale]" if memory.is_stale else ""
            lines.append(f"- [{memory.name}](./{memory.name}.md) — {memory.description}{stale_marker}")

        # 写入项目目录
        index_path = self._get_index_path(self.project_dir)
        index_path.write_text("\n".join(lines), encoding="utf-8")

    def get_index_content(self) -> str:
        """获取索引内容（用于 System Prompt 注入）"""
        memories = self.list_memories()
        lines = []
        for memory in memories:
            lines.append(f"- [{memory.name}](./{memory.name}.md) — {memory.description}")
        return "\n".join(lines)

    def search_memories(self, query: str, limit: int = 5) -> list[Memory]:
        """关键词搜索记忆

        Args:
            query: 搜索关键词
            limit: 返回数量

        Returns:
            list[Memory]: 匹配的记忆
        """
        query_lower = query.lower()
        results = []

        for memory in self.list_memories():
            # 搜索名称、描述、内容
            if (query_lower in memory.name.lower() or
                query_lower in memory.description.lower() or
                query_lower in memory.content.lower()):
                results.append(memory)

        return results[:limit]

    def get_all_memory_content(self, exclude_stale: bool = True) -> str:
        """获取所有记忆的完整内容（用于 System Prompt 注入）

        Args:
            exclude_stale: 是否排除陈旧记忆

        Returns:
            str: 格式化的记忆内容
        """
        memories = self.list_memories()
        if exclude_stale:
            memories = [m for m in memories if not m.is_stale]

        if not memories:
            return ""

        lines = []
        for m in memories:
            lines.append(f"### {m.name}")
            lines.append(f"_{m.description}_")
            lines.append("")
            lines.append(m.content)
            lines.append("")
        return "\n".join(lines)

    def get_selected_memory_content(self, memories: list[Memory]) -> str:
        """获取指定记忆列表的完整内容

        Args:
            memories: 记忆列表（通常由 MemoryRecall 筛选）

        Returns:
            str: 格式化的记忆内容
        """
        if not memories:
            return ""

        lines = []
        for m in memories:
            lines.append(f"### {m.name}")
            lines.append(f"_{m.description}_")
            lines.append("")
            lines.append(m.content)
            lines.append("")
        return "\n".join(lines)

    def update_memory(self, name: str, content: str, description: str | None = None) -> bool:
        """更新记忆内容

        Args:
            name: 记忆名称
            content: 新内容
            description: 新描述（可选）

        Returns:
            bool: 是否成功
        """
        memory = self.get_memory(name)
        if not memory:
            return False

        memory.content = content
        if description:
            memory.description = description
        memory.updated_at = datetime.now()

        # 保存到项目目录（优先）
        return self.save_memory(memory, to_global=False)
