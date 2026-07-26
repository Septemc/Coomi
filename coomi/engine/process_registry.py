"""进程注册表 — 让阻塞式子进程工具（PowerShell/Bash）把活动的 Popen 句柄
登记进来，使 UI 的「停止」动作能够真正 kill 掉底层子进程。

设计要点：
- 线程安全（工具在 asyncio.to_thread 的工作线程里运行，注册/注销发生在工作线程；
  kill_all 由事件循环线程调用）。
- 仅登记「仍在运行」的子进程；进程自然退出后由工具自身注销。
- kill 采用「先 terminate 再 kill」的两段式，尽量给子进程清理机会。
- 与 BackgroundTaskRegistry 解耦：本表只管操作系统层面的进程，不理解任务语义。
"""
from __future__ import annotations

import subprocess
import threading


class ProcessRegistry:
    """记录活动子进程，支持整体强杀。全局单例，供所有 shell 工具共享。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def has_active(self) -> bool:
        with self._lock:
            return any(p.poll() is None for p in self._procs)

    def kill_all(self) -> int:
        """强杀所有仍在运行的子进程，返回被杀数量。

        先 terminate() 给子进程一次收尾机会，随后 kill() 兜底。
        Windows 上 terminate() 等价于 TerminateProcess，kill() 亦然，
        双调用无害；POSIX 上则是 SIGTERM 后 SIGKILL。
        """
        with self._lock:
            procs = list(self._procs)
        killed = 0
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                proc.terminate()
                killed += 1
            except Exception:
                pass
        # 给一个极短的收尾窗口后强杀（不阻塞太久，避免拖住 UI 线程）。
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return killed


# 全局共享单例：shell 工具在模块级引用，避免通过参数层层传递。
PROCESS_REGISTRY = ProcessRegistry()
