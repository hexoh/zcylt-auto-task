"""任务调度：在后台线程里跑整套流程，UI 只管开始/停止和收日志。

线程模型：``start()`` 起一个 daemon 线程，线程内完成
「建日志 → 加载 JSON → 连后端 → 跑 flow → 收尾通知」，
状态变化通过回调抛给 UI，日志同理（UI 侧用队列转主线程）。
"""

from __future__ import annotations

import os
import threading
import time
from enum import Enum
from typing import Callable, List, Optional

from .backends import create_backend
from .logger import Logger
from .notifier import Notifier
from .runner import RunReport, Runner
from .task_schema import load_tasks
from .vision import TemplateMatcher


class TaskStatus(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


StatusCallback = Callable[[TaskStatus], None]
LogCallback = Callable[[str], None]
FinishCallback = Callable[[Optional[RunReport]], None]


class TaskManager:
    def __init__(self):
        self._status = TaskStatus.IDLE
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.on_status: Optional[StatusCallback] = None
        self.on_log: Optional[LogCallback] = None
        self.on_finish: Optional[FinishCallback] = None
        self.logger: Optional[Logger] = None
        self.report: Optional[RunReport] = None

    # ------------------------------------------------------------------ 状态
    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status in (TaskStatus.STARTING, TaskStatus.RUNNING, TaskStatus.STOPPING)

    def set_callbacks(
        self,
        on_status: Optional[StatusCallback] = None,
        on_log: Optional[LogCallback] = None,
        on_finish: Optional[FinishCallback] = None,
    ) -> None:
        self.on_status = on_status
        self.on_log = on_log
        self.on_finish = on_finish

    def _set_status(self, status: TaskStatus) -> None:
        self._status = status
        cb = self.on_status
        if cb:
            try:
                cb(status)
            except Exception:
                pass

    def _emit_log(self, line: str) -> None:
        cb = self.on_log
        if cb:
            try:
                cb(line)
            except Exception:
                pass

    # ------------------------------------------------------------------ 控制
    def start(self, config: dict) -> tuple[bool, str]:
        """启动后台执行。返回 (是否启动成功, 提示信息)。"""
        with self._lock:
            if self.is_running:
                return False, "任务正在运行中"
            self._stop_event.clear()
            self._set_status(TaskStatus.STARTING)
            self._thread = threading.Thread(
                target=self._worker, args=(dict(config),), daemon=True, name="zcylt-runner"
            )
            self._thread.start()
        return True, "已启动"

    def stop(self) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        self._set_status(TaskStatus.STOPPING)
        self._emit_log("正在停止…")

    def wait(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)

    # ------------------------------------------------------------------ 执行
    def _worker(self, config: dict) -> None:
        report: Optional[RunReport] = None
        backend = None
        root = config.get("root") or os.getcwd()
        try:
            logger = Logger(
                log_dir=config.get("log_dir") or os.path.join(root, "logs"),
                level=config.get("level", "info"),
                echo=config.get("echo", True),
                session=config.get("session"),
                # 界面只收中文短句；等级 / key / 坐标写进日志文件
                on_ui=self._emit_log,
            )
            self.logger = logger
            self._set_status(TaskStatus.RUNNING)

            tasks_path = config.get("tasks_path") or os.path.join(root, "tasks")
            taskset = load_tasks(tasks_path, base_dir=root, logger=logger)

            mode = config.get("mode", "adb")
            backend = create_backend(
                mode,
                config.get("backend") or {},
                logger,
                templates_dir=os.path.join(root, "templates"),
            )
            backend.connect()
            logger.info(f"后端就绪：{backend.describe()}")

            matcher = TemplateMatcher(
                threshold=float(taskset.defaults.get("threshold", 0.85)),
                multiscale=bool(config.get("multiscale", False)),
            )
            runner = Runner(
                backend=backend,
                taskset=taskset,
                logger=logger,
                matcher=matcher,
                stop_event=self._stop_event,
                variables=config.get("variables") or {},
                screenshot_on_fail=bool(config.get("screenshot_on_fail", True)),
                selection=config.get("selection") or {},
            )

            flows: List[str] = config.get("flows") or [config.get("flow") or "daily"]
            enabled = config.get("task_keys")
            enabled_set = set(enabled) if enabled is not None else None
            selected: List[str] = []
            for flow in flows:
                for key in taskset.resolve_flow(flow, enabled=enabled_set):
                    if key not in selected:
                        selected.append(key)
            # 勾选了但不在流程里的任务也执行（顺序按 JSON 定义）
            if enabled_set is not None:
                for key in taskset.task_keys():
                    if key in enabled_set and key not in selected:
                        selected.append(key)
            if not selected:
                raise ValueError("没有可执行的任务（可能全部未勾选）")

            logger.info(f"执行计划：{' → '.join(selected)}")
            repeat = max(1, int(config.get("repeat", 1) or 1))
            reports: List[RunReport] = []
            for round_index in range(repeat):
                if self._stop_event.is_set():
                    break
                if repeat > 1:
                    logger.section(f"第 {round_index + 1}/{repeat} 轮")
                reports.append(runner.run(selected))
                if repeat > 1 and round_index + 1 < repeat:
                    time.sleep(1.0)

            report = reports[-1] if reports else RunReport().finish()

        except Exception as exc:  # 任何异常都要落到状态和通知里
            msg = f"{type(exc).__name__}: {exc}"
            if self.logger:
                self.logger.error(msg)
                self.logger.ui(f"执行失败：{msg}", "error")
            else:
                self._emit_log(f"执行失败：{msg}")
            report = RunReport(ok=False, aborted_reason=msg)
            report.errors.append(msg)
            report.finish()
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            self.report = report
            with self._lock:
                self._set_status(TaskStatus.IDLE)
            if report is not None:
                notifier = Notifier(config.get("notify") or {}, self.logger)
                if notifier.enabled:
                    title = "这城有良田 · 任务完成" if report.ok else "这城有良田 · 任务异常"
                    notifier.send_async(title, report.summary)
            cb = self.on_finish
            if cb:
                try:
                    cb(report)
                except Exception:
                    pass


def probe(config: dict, logger: Optional[Logger] = None) -> tuple[bool, str, Optional[str]]:
    """连接自检：连接后端 + 抓一帧 + 存预览图。返回 (成功, 说明, 预览图路径)。"""
    backend = None
    try:
        backend = create_backend(
            config.get("mode", "adb"),
            config.get("backend") or {},
            logger,
            templates_dir=config.get("templates_dir"),
        )
        backend.connect()
        frame = backend.screenshot()
        h, w = frame.shape[:2]
        path = None
        if logger:
            path = logger.save_screenshot(frame, "preview")
        msg = f"{backend.describe()}｜截图 {w}x{h}"
        if logger:
            logger.info(f"连接正常：{msg}")
        return True, msg, path
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if logger:
            logger.error(f"连接失败：{msg}")
        return False, msg, None
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
