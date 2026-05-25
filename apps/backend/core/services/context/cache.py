"""大结果缓存 - 将大型工具结果存入磁盘"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# 缓存配置
CACHE_DIR_NAME = "tool_results"
MAX_CACHE_SIZE_MB = 100  # 最大缓存大小
CACHE_EXPIRY_DAYS = 7    # 缓存过期天数
LARGE_RESULT_THRESHOLD = 50 * 1024  # 50KB 视为大结果


class ToolResultCache:
    """工具结果缓存

    将大型工具结果存入磁盘，避免占用上下文窗口。

    存储位置：
    - ~/.coomi/cache/tool_results/ - 全局缓存
    - .coomi/cache/tool_results/ - 项目缓存（优先级高）
    """

    def __init__(self, project_path: str | None = None):
        """
        Args:
            project_path: 项目根目录路径
        """
        self.global_cache_dir = self._get_global_cache_dir()
        self.project_cache_dir = self._get_project_cache_dir(project_path)

        # 确保目录存在
        self._ensure_dir(self.global_cache_dir)
        self._ensure_dir(self.project_cache_dir)

    def _get_global_cache_dir(self) -> Path:
        """获取全局缓存目录"""
        return Path.home() / ".coomi" / "cache" / CACHE_DIR_NAME

    def _get_project_cache_dir(self, project_path: str | None) -> Path:
        """获取项目缓存目录"""
        if not project_path:
            return self.global_cache_dir
        return Path(project_path) / ".coomi" / "cache" / CACHE_DIR_NAME

    def _ensure_dir(self, dir_path: Path) -> None:
        """确保目录存在"""
        dir_path.mkdir(parents=True, exist_ok=True)

    def _generate_key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """生成缓存键"""
        # 使用工具名和参数生成唯一键
        key_data = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def get(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """获取缓存的工具结果

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            str | None: 缓存的结果，不存在则返回 None
        """
        key = self._generate_key(tool_name, arguments)

        # 先查项目缓存
        result = self._load_from_dir(self.project_cache_dir, key)
        if result:
            return result

        # 再查全局缓存
        return self._load_from_dir(self.global_cache_dir, key)

    def _load_from_dir(self, cache_dir: Path, key: str) -> str | None:
        """从指定目录加载缓存"""
        filepath = cache_dir / f"{key}.json"
        if not filepath.exists():
            return None

        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            # 检查过期
            created = datetime.fromisoformat(data["created"])
            if datetime.now() - created > timedelta(days=CACHE_EXPIRY_DAYS):
                # 过期，删除
                filepath.unlink()
                return None
            return data["result"]
        except Exception:
            return None

    def put(self, tool_name: str, arguments: dict[str, Any], result: str, force: bool = False) -> bool:
        """缓存工具结果

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            result: 工具结果
            force: 是否强制缓存（即使结果较小）

        Returns:
            bool: 是否成功缓存
        """
        if not result:
            return False
        # 检查是否需要缓存
        if not force and len(result.encode()) < LARGE_RESULT_THRESHOLD:
            return False

        key = self._generate_key(tool_name, arguments)
        cache_data = {
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "created": datetime.now().isoformat(),
            "size": len(result.encode()),
        }

        # 优先缓存到项目目录
        target_dir = self.project_cache_dir
        filepath = target_dir / f"{key}.json"

        try:
            filepath.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def evict_expired(self) -> int:
        """清理过期缓存

        Returns:
            int: 清理的缓存数量
        """
        count = 0
        for cache_dir in [self.project_cache_dir, self.global_cache_dir]:
            if not cache_dir.exists():
                continue

            for filepath in cache_dir.glob("*.json"):
                try:
                    data = json.loads(filepath.read_text(encoding="utf-8"))
                    created = datetime.fromisoformat(data["created"])
                    if datetime.now() - created > timedelta(days=CACHE_EXPIRY_DAYS):
                        filepath.unlink()
                        count += 1
                except Exception:
                    continue

        return count

    def get_cache_size(self) -> dict[str, int]:
        """获取缓存大小

        Returns:
            dict: 包含项目缓存和全局缓存的大小（字节）
        """
        def dir_size(dir_path: Path) -> int:
            if not dir_path.exists():
                return 0
            return sum(f.stat().st_size for f in dir_path.glob("*.json") if f.is_file())

        return {
            "project": dir_size(self.project_cache_dir),
            "global": dir_size(self.global_cache_dir),
        }

    def clear(self, include_global: bool = False) -> int:
        """清空缓存

        Args:
            include_global: 是否清空全局缓存

        Returns:
            int: 清理的缓存数量
        """
        count = 0
        dirs_to_clear = [self.project_cache_dir]
        if include_global:
            dirs_to_clear.append(self.global_cache_dir)

        for cache_dir in dirs_to_clear:
            if not cache_dir.exists():
                continue
            for filepath in cache_dir.glob("*.json"):
                try:
                    filepath.unlink()
                    count += 1
                except Exception:
                    continue

        return count

    def get_summary(self, tool_name: str, arguments: dict[str, Any], max_length: int = 200) -> str | None:
        """获取缓存结果的摘要

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            max_length: 最大摘要长度

        Returns:
            str | None: 结果摘要
        """
        result = self.get(tool_name, arguments)
        if not result:
            return None

        if len(result) <= max_length:
            return result

        return result[:max_length] + f"\n... [缓存结果，共 {len(result)} 字符]"
