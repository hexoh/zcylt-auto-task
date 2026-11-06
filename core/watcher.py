"""循环监控（方案 A：空闲巡检）。

一句话：**主流程不跑的时候**，每隔一段时间截一帧，看图里有没有「某个东西」；
出现就跑一条指定的 JSON 任务（比如领红包），跑完回到待机。

设计成「多规则」是因为这类需求不止红包一个——签到弹窗、限时活动、体力溢出提示，
本质都是「周期性看一眼 → 命中就跑一条任务」。所以规则是数据（``config.json``
的 ``watchers.rules``），引擎只做三件事：排期、识图、跑任务。

关键约定：
* **同一时刻只有一条动作流**：巡检与领取全程持有 :class:`DeviceGate`，
  主流程开始时会在门上等（见 ``core/device.py``）。
* **命中要确认两帧**：动画/半透明遮罩容易骗过单帧匹配。
* **冷却 + 配额**：同一规则 ``cooldown_seconds`` 内不重复领，``max_per_hour`` 封顶，
  防止「识别卡住 → 无限点击」。
* **失败退避**：截图/设备异常按 30s→60s→120s 退避，连续 5 次失败自动停机并报原因。
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from .backends import create_backend
from .device import DeviceGate
from .errors import BackendError
from .logger import Logger
from .runner import Runner
from .vision import TemplateMatcher

#: 巡检间隔下限，防止配置写 0.5 秒把设备刷爆
MIN_INTERVAL = 5.0
MAX_BACKOFF = 300.0
MAX_FAILURES = 5

RULE_DEFAULTS = {
    "enabled": True,
    "interval_seconds": 300,
    "threshold": 0.85,
    "confirm_frames": 2,
    "cooldown_seconds": 600,
    "max_per_hour": 6,
    "region": None,
    "jitter_seconds": 5,
    "grayscale": True,
    "multiscale": False,
}

def describe_interval(seconds: float) -> str:
    """300 → 「5 分钟」，90 → 「1.5 分钟」，便于界面显示。"""
    if seconds >= 60:
        minutes = seconds / 60
        text = f"{minutes:.0f}" if abs(minutes - round(minutes)) < 0.05 else f"{minutes:.1f}"
        return f"{text} 分钟"
    return f"{seconds:.0f} 秒"


@dataclass
class WatchRule:
    key: str = ""
    name: str = ""
    task: str = ""
    template: str = ""
    enabled: bool = True
    interval_seconds: float = 300.0
    threshold: float = 0.85
    confirm_frames: int = 2
    cooldown_seconds: float = 600.0
    max_per_hour: int = 6
    region: Optional[List[int]] = None
    jitter_seconds: float = 5.0
    grayscale: bool = True
    multiscale: bool = False

    @property
    def title(self) -> str:
        return self.name or self.key

    def interval_label(self) -> str:
        return describe_interval(self.interval_seconds)

    def next_delay(self) -> float:
        """带抖动的等待时长：避免和游戏刷新节拍长期对齐。"""
        jitter = max(0.0, float(self.jitter_seconds))
        return self.interval_seconds + random.uniform(-jitter, jitter) if jitter else self.interval_seconds

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "task": self.task,
            "template": self.template,
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "threshold": self.threshold,
            "confirm_frames": self.confirm_frames,
            "cooldown_seconds": self.cooldown_seconds,
            "max_per_hour": self.max_per_hour,
            "region": list(self.region) if self.region else None,
            "jitter_seconds": self.jitter_seconds,
            "grayscale": self.grayscale,
            "multiscale": self.multiscale,
        }


def parse_rules(raw_rules: Optional[Sequence[dict]]) -> Tuple[List[WatchRule], List[str]]:
    """``config.json → watchers.rules`` → 规则对象。坏规则跳过并给出原因。"""
    rules: List[WatchRule] = []
    problems: List[str] = []
    seen = set()

    for index, raw in enumerate(raw_rules or []):
        if not isinstance(raw, dict):
            problems.append(f"第 {index + 1} 条规则不是对象，已忽略")
            continue
        key = str(raw.get("key") or "").strip() or f"rule{index + 1}"
        if key in seen:
            problems.append(f"规则 key 重复：{key}，已忽略后一条")
            continue
        task = str(raw.get("task") or "").strip()
        template = str(raw.get("template") or "").strip()
        if not task or not template:
            problems.append(f"{key}：task / template 都不能为空，已忽略")
            continue

        interval = raw.get("interval_seconds", RULE_DEFAULTS["interval_seconds"])
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            problems.append(f"{key}：interval_seconds 不是数字，用默认 300 秒")
            interval = RULE_DEFAULTS["interval_seconds"]
        if interval < MIN_INTERVAL:
            problems.append(f"{key}：interval_seconds 太小（<{MIN_INTERVAL:g}s），已按下限处理")
            interval = MIN_INTERVAL

        region = raw.get("region")
        if region is not None:
            if (
                isinstance(region, (list, tuple))
                and len(region) == 4
                and all(isinstance(v, (int, float)) for v in region)
            ):
                region = [int(v) for v in region]
            else:
                problems.append(f"{key}：region 必须是 [x, y, w, h]，已忽略")
                region = None

        rules.append(
            WatchRule(
                key=key,
                name=str(raw.get("name") or key),
                task=task,
                template=template.replace("\\", "/"),
                enabled=bool(raw.get("enabled", RULE_DEFAULTS["enabled"])),
                interval_seconds=interval,
                threshold=float(raw.get("threshold", RULE_DEFAULTS["threshold"]) or RULE_DEFAULTS["threshold"]),
                confirm_frames=max(1, int(raw.get("confirm_frames", RULE_DEFAULTS["confirm_frames"]) or 1)),
                cooldown_seconds=float(
                    raw.get("cooldown_seconds", RULE_DEFAULTS["cooldown_seconds"]) or 0
                ),
                max_per_hour=max(0, int(raw.get("max_per_hour", RULE_DEFAULTS["max_per_hour"]) or 0)),
                region=region,
                jitter_seconds=float(raw.get("jitter_seconds", RULE_DEFAULTS["jitter_seconds"]) or 0),
                grayscale=bool(raw.get("grayscale", RULE_DEFAULTS["grayscale"])),
                multiscale=bool(raw.get("multiscale", RULE_DEFAULTS["multiscale"])),
            )
        )
        seen.add(key)
    return rules, problems


@dataclass
class RuleRuntime:
    """一条规则的运行时状态（排期、计数、给界面看的一句话）。"""

    rule: WatchRule
    next_at: float = 0.0
    last_check: Optional[float] = None
    last_score: float = 0.0
    last_hit: Optional[float] = None
    hits: int = 0
    note: str = "待机"
    hit_times: Deque[float] = field(default_factory=lambda: deque(maxlen=128))

    def can_claim(self, now: float) -> Tuple[bool, str]:
        if self.rule.cooldown_seconds > 0 and self.last_hit:
            remain = self.rule.cooldown_seconds - (now - self.last_hit)
            if remain > 0:
                return False, f"冷却中，还需 {int(remain)} 秒"
        if self.rule.max_per_hour > 0:
            recent = [t for t in self.hit_times if now - t <= 3600]
            if len(recent) >= self.rule.max_per_hour:
                return False, f"已达每小时上限 {self.rule.max_per_hour} 次"
        return True, ""

    def record_hit(self, now: float, ok: bool = True) -> None:
        """记一次命中；``ok=False``（领取失败）只上冷却，不增加「已领」计数。"""
        if ok:
            self.hits += 1
        self.last_hit = now
        self.hit_times.append(now)

    def snapshot(self) -> dict:
        return {
            "key": self.rule.key,
            "name": self.rule.title,
            "task": self.rule.task,
            "enabled": self.rule.enabled,
            "interval": self.rule.interval_label(),
            "note": self.note,
            "last_check": self.last_check,
            "last_score": self.last_score,
            "hits": self.hits,
            "last_hit": self.last_hit,
        }


class WatcherService:
    """空闲巡检服务：一个线程管所有规则。"""

    def __init__(
        self,
        root_dir: str,
        *,
        gate: Optional[DeviceGate] = None,
        on_log: Optional[Callable[[str], None]] = None,
        is_busy: Optional[Callable[[], bool]] = None,
    ):
        self.root_dir = root_dir
        self.templates_dir = os.path.join(root_dir, "templates")
        self._gate = gate or DeviceGate()
        self._on_log = on_log
        self._is_busy = is_busy or (lambda: False)

        self._runtimes: List[RuleRuntime] = []
        self._rules_problems: List[str] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._backend = None
        self._backend_cfg: dict = {}
        self._mode = ""
        self._taskset = None
        self._logger: Optional[Logger] = None
        self._matcher: Optional[TemplateMatcher] = None
        self._variables: Dict[str, str] = {}

        self._running = False
        self._message = "未启动"
        self._failures = 0
        self._claiming = False

    # ------------------------------------------------------------------ 配置
    def configure(
        self,
        rules: Sequence[WatchRule],
        *,
        mode: str,
        backend: dict,
        taskset,
        variables: Optional[dict] = None,
        problems: Optional[Sequence[str]] = None,
    ) -> None:
        """换一套规则 / 后端参数（重启监控后生效）。"""
        with self._lock:
            self._runtimes = [RuleRuntime(rule=rule) for rule in rules]
            self._rules_problems = list(problems or [])
            self._mode = mode
            self._backend_cfg = dict(backend or {})
            self._taskset = taskset
            self._variables = dict(variables or {})

    def set_backend(self, mode: str, backend: dict) -> None:
        """方案切换 / 设置保存后同步参数（正在跑的话由调用方负责重启）。"""
        with self._lock:
            self._mode = mode
            self._backend_cfg = dict(backend or {})
            if self._matcher is not None:
                self._matcher.clear_cache()

    @property
    def rules(self) -> List[WatchRule]:
        return [runtime.rule for runtime in self._runtimes]

    @property
    def problems(self) -> List[str]:
        return list(self._rules_problems)

    # ------------------------------------------------------------------ 状态
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def claiming(self) -> bool:
        return self._claiming

    def state(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "message": self._message,
                "problems": list(self._rules_problems),
                "rules": [runtime.snapshot() for runtime in self._runtimes],
            }

    # ------------------------------------------------------------------ 起停
    def start(self) -> Tuple[bool, str]:
        with self._lock:
            if self._running:
                return True, "监控已在运行"
            enabled = [rt for rt in self._runtimes if rt.rule.enabled]
            if not enabled:
                return False, self._fail_start("没有启用的监控规则")
            if (self._mode or "").lower() == "mock":
                return False, self._fail_start("演练模式不启用监控（识图结果不可信）")
            if self._taskset is None:
                return False, self._fail_start("任务配置还没加载好")

            missing: List[str] = []
            for rt in enabled:
                if rt.rule.task not in self._taskset.tasks:
                    return False, self._fail_start(
                        f"规则「{rt.rule.title}」指向的任务不存在：{rt.rule.task}"
                    )
                path = self._template_path(rt.rule)
                if not os.path.isfile(path):
                    missing.append(rt.rule.template)
            if missing:
                return False, self._fail_start(
                    "缺少模板图：" + "、".join(missing) + "（用任务添加器补拍后再开启）"
                )

            try:
                self._logger = Logger(
                    log_dir=os.path.join(self.root_dir, "logs"),
                    level="info",
                    echo=False,
                    session="watch",
                    on_ui=self._emit,
                )
                self._matcher = TemplateMatcher(
                    threshold=float((self._taskset.defaults or {}).get("threshold", 0.85))
                )
                self._backend = create_backend(
                    self._mode,
                    self._backend_cfg,
                    self._logger,
                    templates_dir=self.templates_dir,
                )
                self._backend.connect()
            except Exception as exc:
                self._close_backend()
                self._message = f"启动失败：{exc}"
                self._emit(f"监控启动失败：{exc}")
                return False, self._message

            self._stop.clear()
            self._failures = 0
            now = time.monotonic()
            for rt in self._runtimes:
                rt.next_at = now  # 启动先扫一轮，不用等满一个周期
                rt.note = "待机"
            self._running = True
            self._message = f"巡检中（{len(enabled)} 条规则）"
            self._thread = threading.Thread(target=self._loop, daemon=True, name="zcylt-watch")
            self._thread.start()
            self._emit(f"监控已启动：{len(enabled)} 条规则")
            return True, self._message

    def _fail_start(self, message: str) -> str:
        self._message = message
        return message

    def stop(self, reason: str = "") -> None:
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop.set()
            self._message = reason or "已停止"
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._close_backend()
        if was_running:
            self._emit(f"监控已停止：{self._message}")

    def restart(self) -> Tuple[bool, str]:
        self.stop("重启中")
        return self.start()

    # ------------------------------------------------------------------ 巡检
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._is_busy():
                    self._message = "主流程运行中，监控暂停"
                    self._wait(1.0)
                    continue

                now = time.monotonic()
                due = [rt for rt in self._runtimes if rt.rule.enabled and now >= rt.next_at]
                if not due:
                    upcoming = [rt.next_at for rt in self._runtimes if rt.rule.enabled]
                    wait = (min(upcoming) - now) if upcoming else 5.0
                    self._wait(max(0.2, min(wait, 2.0)))
                    continue

                self._message = "巡检中"
                frame = self._grab()
                for runtime in due:
                    if self._stop.is_set():
                        break
                    self._inspect(runtime, frame)
                    runtime.next_at = time.monotonic() + runtime.rule.next_delay()
                self._failures = 0
            except BackendError as exc:
                self._handle_failure(f"设备异常：{exc}")
            except Exception as exc:  # 兜底：监控线程不能死
                self._handle_failure(f"巡检异常：{type(exc).__name__}: {exc}")

    def _inspect(self, runtime: RuleRuntime, frame) -> None:
        """看一帧：命中 → 确认 → 冷却/配额 → 领。"""
        rule = runtime.rule
        runtime.last_check = time.time()
        match = self._find(frame, rule)
        runtime.last_score = match.score if match else 0.0

        if match is None:
            runtime.note = f"未发现（最高 {runtime.last_score:.2f}）"
            return

        if rule.confirm_frames > 1:
            time.sleep(0.25)
            again = self._find(self._grab(), rule)
            if again is None:
                runtime.note = "只闪了一帧，判定为误报"
                runtime.last_score = 0.0
                return

        now = time.time()
        allowed, why = runtime.can_claim(now)
        if not allowed:
            runtime.note = why
            return

        self._claim(runtime)

    def _claim(self, runtime: RuleRuntime) -> None:
        rule = runtime.rule
        with self._gate.hold(f"监控·{rule.title}", timeout=12.0) as locked:
            if not locked:
                runtime.note = "设备被占用，下轮再试"
                return
            self._claiming = True
            runtime.note = "领取中…"
            try:
                runner = Runner(
                    backend=self._backend,
                    taskset=self._taskset,
                    logger=self._logger,
                    matcher=self._matcher,
                    stop_event=self._stop,
                    variables=dict(self._variables),
                    selection={},
                )
                report = runner.run([rule.task])
                runtime.record_hit(time.time(), ok=bool(report.ok))
                runtime.note = "已领取" if report.ok else "领取失败"
            except Exception as exc:
                runtime.note = f"领取异常：{exc}"
                self._emit(f"{rule.title} 领取异常：{exc}")
            finally:
                self._claiming = False

    def _find(self, frame, rule: WatchRule):
        if frame is None or self._matcher is None:
            return None
        return self._matcher.find(
            frame,
            self._template_path(rule),
            threshold=rule.threshold,
            region=rule.region,
            grayscale=rule.grayscale,
            multiscale=rule.multiscale,
        )

    def _template_path(self, rule: WatchRule) -> str:
        path = rule.template
        return path if os.path.isabs(path) else os.path.join(self.root_dir, path)

    def _grab(self):
        if self._backend is None:
            raise BackendError("监控后端未就绪")
        frame = self._backend.screenshot()
        if frame is None or getattr(frame, "size", 0) == 0:
            raise BackendError("截图返回空画面")
        return frame

    # ------------------------------------------------------------------ 立即检测
    def check_now(self, key: Optional[str] = None) -> List[dict]:
        """手动「立即检测」：只跑一次，不改变排期。返回每条规则的结果。"""
        results: List[dict] = []
        with self._lock:
            targets = [rt for rt in self._runtimes if rt.rule.enabled and (key is None or rt.rule.key == key)]
            if not self._running or self._backend is None:
                return [{"key": rt.rule.key, "note": "监控未启动"} for rt in targets]
        if self._is_busy():
            return [{"key": rt.rule.key, "note": "主流程运行中，稍后再试"} for rt in targets]

        try:
            frame = self._grab()
        except Exception as exc:
            return [{"key": rt.rule.key, "note": f"截图失败：{exc}"} for rt in targets]

        for runtime in targets:
            try:
                self._inspect(runtime, frame)
            except Exception as exc:
                runtime.note = f"检测失败：{exc}"
            results.append({"key": runtime.rule.key, "note": runtime.note, "score": runtime.last_score})
        return results

    # ------------------------------------------------------------------ 内部
    def _handle_failure(self, message: str) -> None:
        self._failures += 1
        delay = min(MAX_BACKOFF, 30.0 * (2 ** (self._failures - 1)))
        now = time.monotonic()
        for runtime in self._runtimes:
            runtime.next_at = now + delay
            runtime.note = message
            runtime.last_check = time.time()
        self._emit(f"{message}（第 {self._failures} 次，{int(delay)} 秒后重试）")
        self._message = f"异常重试中（{int(delay)}s）"
        if self._failures >= MAX_FAILURES:
            self.stop("连续 5 次设备异常，监控已暂停")

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    def _emit(self, line: str) -> None:
        if self._on_log:
            try:
                self._on_log(line)
            except Exception:
                pass
