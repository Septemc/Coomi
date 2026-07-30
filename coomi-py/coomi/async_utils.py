"""异步工具"""
from __future__ import annotations

import asyncio
from typing import Any


async def run_in_thread(func, *args: Any, **kwargs: Any) -> Any:
    """在线程池中执行同步函数，返回结果"""
    return await asyncio.to_thread(func, *args, **kwargs)
