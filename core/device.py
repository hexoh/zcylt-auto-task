"""设备互斥：同一时刻只允许一个"动作流"操作后端。

为什么需要它
------------
主流程（``TaskManager``）和循环监控（``core/watcher.py``）都会截图 + 点击。两者一旦
交替执行，点击序列会被打断（滑动拖到一半、输入框被抢焦点），表现为"偶发失败"，
而且极难复现。

方案 A 的约定是「主流程跑的时候监控只休息」，但两件事之间仍有竞态：
用户点「开始」的瞬间监控正好命中红包。所以除了「先看 is_running」的判断，
还需要一把真正的锁——谁拿到 ``DeviceGate`` 谁才能动设备，另一侧等它放。

锁是可重入的（``RLock``）：同一个线程里 Runner 的嵌套调用不会自己把自己锁死。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


class DeviceGate:
    """一把带「当前持有者」标注的可重入锁。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner = ""

    # ------------------------------------------------------------------ 查询
    @property
    def owner(self) -> str:
        """当前占用设备的任务名（空串 = 空闲）。"""
        return self._owner

    @property
    def busy(self) -> bool:
        return bool(self._owner)

    # ------------------------------------------------------------------ 使用
    def acquire(self, owner: str = "", timeout: Optional[float] = None) -> bool:
        """尝试拿锁；``timeout=None`` 表示一直等。返回是否拿到。"""
        if timeout is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=max(0.0, float(timeout)))
        if acquired:
            self._owner = owner or self._owner
        return acquired

    def release(self) -> None:
        try:
            self._lock.release()
        finally:
            self._owner = ""

    @contextmanager
    def hold(self, owner: str = "", timeout: Optional[float] = None) -> Iterator[bool]:
        """``with gate.hold("主流程", timeout=30) as ok:`` —— ok 为是否拿到锁。"""
        acquired = self.acquire(owner, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()
