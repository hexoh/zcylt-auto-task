"""日志系统：文件/控制台留细节，界面只留一句结果。

两条通道，互不干扰：

- :meth:`Logger.log`：写文件 + 控制台，带 ``[HH:MM:SS] [INFO]`` 前缀和任务 key，
  是给开发者排查用的。
- :meth:`Logger.ui`：只推给界面，形如 ``[10:31:07] 收取税收 成功``，
  不出现等级、key、坐标、模板名（见 FEATURES.md 1.4）。

失败的截图落在 ``logs/screenshots/``，方便回看是哪一帧识别挂了。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional, Tuple

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

LogCallback = Callable[[str], None]


class Logger:
    def __init__(
        self,
        log_dir: Optional[str] = None,
        on_log: Optional[LogCallback] = None,
        level: str = "info",
        echo: bool = True,
        session: Optional[str] = None,
        on_ui: Optional[LogCallback] = None,
    ):
        self.log_dir = os.path.abspath(log_dir or os.path.join(os.getcwd(), "logs"))
        os.makedirs(self.log_dir, exist_ok=True)
        self.screenshot_dir = os.path.join(self.log_dir, "screenshots")
        self.level = LEVELS.get(str(level).lower(), 20)
        self.echo = echo
        #: 详细行回调（等级 / key / 坐标），不接界面
        self.on_log = on_log
        #: 界面短句回调，形如 ``[10:31:07] 收取税收 成功``
        self.on_ui = on_ui
        self.records: List[Tuple[float, str, str]] = []
        self.ui_records: List[str] = []
        self._lock = threading.Lock()
        self.started_at = time.time()

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session = session or f"session_{stamp}"
        self.path = os.path.join(self.log_dir, f"{self.session}.log")
        try:
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(f"===== {self.session} @ {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ 输出
    def _write(self, line: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass

    def log(self, message: str, level: str = "info") -> None:
        """写文件 + 控制台，带等级；**不推界面**（界面走 :meth:`ui`）。"""
        level = str(level).lower()
        if LEVELS.get(level, 20) < self.level:
            return
        text = str(message)
        line = f"[{datetime.now():%H:%M:%S}] [{level.upper()}] {text}"
        with self._lock:
            self.records.append((time.time(), level, text))
            if len(self.records) > 5000:
                del self.records[:2000]
            self._write(line)
        if self.echo:
            print(line, flush=True)
        if self.on_log:
            try:
                self.on_log(line)
            except Exception:
                pass

    def ui(self, message: str, level: str = "info") -> None:
        """界面专用短句：``[10:31:07] 收取税收 成功``。

        - 不带等级、不带任务 key、不带坐标——细节留在 :meth:`log` 的文件里
        - 同时把带等级的同一句写进日志文件，便于事后对照
        """
        text = str(message)
        short = f"[{datetime.now():%H:%M:%S}] {text}"
        with self._lock:
            self.ui_records.append(short)
            if len(self.ui_records) > 2000:
                del self.ui_records[:800]
            self._write(f"[{datetime.now():%H:%M:%S}] [{str(level).upper()}] {text}")
        cb = self.on_ui
        if cb:
            try:
                cb(short)
            except Exception:
                pass

    def debug(self, message: str) -> None:
        self.log(message, "debug")

    def info(self, message: str) -> None:
        self.log(message, "info")

    def warn(self, message: str) -> None:
        self.log(message, "warn")

    def error(self, message: str) -> None:
        self.log(message, "error")

    def section(self, title: str) -> None:
        self.log(f"---------- {title} ----------", "info")

    # ------------------------------------------------------------------ 截图
    def save_screenshot(self, frame, name: str = "shot") -> Optional[str]:
        """保存失败现场截图，返回路径（缺依赖时返回 None）。"""
        if frame is None:
            return None
        try:
            from . import vision
        except Exception:
            return None
        if not vision.CV_AVAILABLE:
            return None
        os.makedirs(self.screenshot_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))[:60]
        path = os.path.join(
            self.screenshot_dir, f"{safe}_{datetime.now():%H%M%S_%f}.png"
        )
        try:
            ok, buf = vision.cv2.imencode(".png", frame)
            if not ok:
                return None
            buf.tofile(path)
            return path
        except Exception:
            return None
