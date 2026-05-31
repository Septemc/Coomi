"""记忆类型定义"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MemoryType(Enum):
    """记忆类型"""
    USER = "user"           # 用户偏好、角色、知识
    FEEDBACK = "feedback"   # 行为反馈（纠正或确认）
    PROJECT = "project"     # 项目动态、目标、决策
    REFERENCE = "reference" # 外部资源指针


@dataclass
class Memory:
    """记忆条目"""
    name: str                    # 唯一标识（kebab-case）
    description: str             # 一行摘要（用于召回筛选）
    memory_type: MemoryType
    content: str                 # 记忆内容
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    is_stale: bool = False       # 陈旧标记

    def to_frontmatter(self) -> str:
        """转换为 frontmatter 格式"""
        return f"""---
name: {self.name}
description: {self.description}
type: {self.memory_type.value}
created: {self.created_at.isoformat()}
updated: {self.updated_at.isoformat()}
---

{self.content}"""

    @classmethod
    def from_file(cls, filepath: str, content: str) -> Memory | None:
        """从文件内容解析记忆"""
        import re

        # 解析 frontmatter
        match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
        if not match:
            return None

        frontmatter = match.group(1)
        body = match.group(2).strip()

        # 解析字段
        name = ""
        description = ""
        memory_type = MemoryType.USER
        created = datetime.now()
        updated = datetime.now()

        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                if key == 'name':
                    name = value
                elif key == 'description':
                    description = value
                elif key == 'type':
                    try:
                        memory_type = MemoryType(value)
                    except ValueError:
                        memory_type = MemoryType.USER
                elif key == 'created':
                    try:
                        created = datetime.fromisoformat(value)
                    except ValueError:
                        pass
                elif key == 'updated':
                    try:
                        updated = datetime.fromisoformat(value)
                    except ValueError:
                        pass

        if not name:
            return None

        return cls(
            name=name,
            description=description,
            memory_type=memory_type,
            content=body,
            created_at=created,
            updated_at=updated,
        )
