"""执行器：解释 JSON 里的 step 并按语义操作后端。

语义要点：

- ``times`` / ``interval``：点击次数与间隔
- ``timeout``：识图类动作的等待上限（超时算失败）
- ``on_fail``：``continue`` 继续下一步 / ``abort`` 中止整个流程 / ``next_task`` 跳到下一个任务
- ``after``：本步完成后固定等待
- ``call`` / ``loop`` / ``exists``：组合与分支，带递归深度和循环引用保护
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .backends import Backend
from .errors import AbortRun, BackendError, NextTask, StepFailed, TemplateMissing, VisionUnavailable
from .task_schema import TaskSet
from .vision import Match, TemplateMatcher

MAX_CALL_DEPTH = 8


@dataclass
class RunReport:
    ok: bool = True
    tasks_total: int = 0
    tasks_ok: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0
    steps_ok: int = 0
    steps_failed: int = 0
    started_at: float = field(default_factory=time.time)
    duration: float = 0.0
    aborted_reason: str = ""
    errors: List[str] = field(default_factory=list)

    def finish(self) -> "RunReport":
        self.duration = time.time() - self.started_at
        self.ok = self.ok and self.tasks_failed == 0 and not self.aborted_reason
        return self

    @property
    def summary(self) -> str:
        state = "完成" if self.ok else "异常结束"
        parts = [
            f"任务 {self.tasks_ok}/{self.tasks_total} 成功",
            f"失败 {self.tasks_failed}",
            f"跳过 {self.tasks_skipped}",
            f"步骤 {self.steps_ok} 成功 / {self.steps_failed} 失败",
            f"耗时 {self.duration:.1f}s",
        ]
        line = f"{state} · " + "，".join(parts)
        if self.aborted_reason:
            line += f"（{self.aborted_reason}）"
        return line

    @property
    def brief(self) -> str:
        """界面用的一句话总结（不带步骤数、失败原因等细节）。"""
        if self.ok:
            return f"全部完成：任务 {self.tasks_ok}/{self.tasks_total} 成功，耗时 {self.duration:.1f}s"
        return (
            f"执行结束：成功 {self.tasks_ok}，失败 {self.tasks_failed}，"
            f"耗时 {self.duration:.1f}s"
        )


class Runner:
    def __init__(
        self,
        backend: Backend,
        taskset: TaskSet,
        logger,
        matcher: Optional[TemplateMatcher] = None,
        stop_event: Optional[threading.Event] = None,
        variables: Optional[Dict[str, str]] = None,
        screenshot_on_fail: bool = True,
        poll_interval: float = 0.35,
        selection: Optional[Dict[str, Sequence[str]]] = None,
    ):
        self.backend = backend
        self.taskset = taskset
        self.logger = logger
        self.matcher = matcher or TemplateMatcher(
            threshold=taskset.defaults.get("threshold", 0.85)
        )
        self.stop_event = stop_event or threading.Event()
        self.variables = dict(variables or {})
        #: 点位勾选结果：{"reward_rows": ["r1", "r3"]}
        self.selection: Dict[str, List[str]] = {
            str(k): list(v) for k, v in (selection or {}).items()
        }
        self.screenshot_on_fail = screenshot_on_fail
        self.poll_interval = poll_interval
        self.report = RunReport()
        self._stack: List[str] = []
        self._last_frame = None
        self._current_task = ""

        self._handlers = {
            "tap": self._do_tap,
            "image_tap": self._do_image_tap,
            "tap_list": self._do_tap_list,
            "swipe": self._do_swipe,
            "text": self._do_text,
            "keyevent": self._do_keyevent,
            "sleep": self._do_sleep,
            "wait_image": self._do_wait_image,
            "wait_gone": self._do_wait_gone,
            "exists": self._do_exists,
            "call": self._do_call,
            "loop": self._do_loop,
            "launch": self._do_launch,
            "kill": self._do_kill,
        }

    # ------------------------------------------------------------------ 对外
    def run(self, task_keys: Sequence[str]) -> RunReport:
        self.report = RunReport(tasks_total=len(task_keys))
        self.logger.section("开始执行")
        try:
            for key in task_keys:
                try:
                    # 放在 try 里：刚开始就按了停止也要落成「异常结束」而不是抛到调用方
                    self._check_stop()
                    self.run_task(key)
                except AbortRun as exc:
                    self.report.aborted_reason = str(exc)
                    self.logger.error(f"流程中止：{exc}")
                    break
                except BackendError as exc:
                    self.report.ok = False
                    self.report.errors.append(str(exc))
                    self.report.aborted_reason = "后端错误"
                    self.logger.error(f"后端错误，流程中止：{exc}")
                    break
                except Exception as exc:  # 兜底，避免静默死掉
                    self.report.ok = False
                    self.report.errors.append(str(exc))
                    self.logger.error(f"未预期异常，流程中止：{exc}\n{traceback.format_exc()}")
                    break
        finally:
            self.report.finish()
            self.logger.section(self.report.summary)
            # 界面只留一句总结，过程细节在日志文件里
            self.logger.ui(self.report.brief)
        return self.report

    def run_task(self, key: str, depth: int = 0) -> bool:
        task = self.taskset.tasks.get(key)
        if task is None:
            self.report.tasks_failed += 1
            self.logger.error(f"任务不存在：{key}")
            return False
        if not task.enabled:
            self.report.tasks_skipped += 1
            self.logger.warn(f"↷ 任务已禁用，跳过：{key}")
            return True
        if depth > MAX_CALL_DEPTH:
            self.report.tasks_failed += 1
            self.logger.error(f"call 嵌套超过 {MAX_CALL_DEPTH} 层，疑似循环：{key}")
            return False
        if key in self._stack:
            self.report.tasks_failed += 1
            self.logger.error(f"call 循环引用：{' → '.join(self._stack + [key])}")
            return False

        self._stack.append(key)
        self._current_task = key
        started = time.time()
        self.logger.info(f"▶ 任务 {key}｜{task.title}")
        skipped = False
        try:
            ok = self._run_steps(task.steps, owner=key, depth=depth)
        except NextTask:
            if depth > 0:
                raise
            # 跳过既不算成功也不算失败，两者不能同时加
            self.report.tasks_skipped += 1
            self.logger.warn(f"↷ 跳过任务 {key}（on_fail=next_task）")
            skipped = True
            ok = True
        finally:
            self._stack.pop()
            self._current_task = self._stack[-1] if self._stack else ""

        if skipped:
            self.logger.info(f"↷ 任务跳过 {key}（{time.time() - started:.1f}s）")
            return True

        cost = time.time() - started
        if ok:
            self.report.tasks_ok += 1
            self.logger.info(f"✔ 任务完成 {key}（{cost:.1f}s）")
            if depth == 0:
                self.logger.ui(f"{task.title} 成功")
        else:
            self.report.tasks_failed += 1
            self.logger.error(f"✘ 任务失败 {key}（{cost:.1f}s）")
            if depth == 0:
                self.logger.ui(f"{task.title} 失败", "error")
        return ok

    # ------------------------------------------------------------------ 步骤
    def _run_steps(self, steps: Sequence[dict], owner: str, depth: int) -> bool:
        all_ok = True
        for index, step in enumerate(steps):
            self._check_stop()
            action = step.get("action", "?")
            label = step.get("id") or step.get("comment") or f"#{index + 1}"
            try:
                ok = self._run_step(step, owner, depth)
            except (NextTask, AbortRun):
                raise
            except BackendError:
                raise
            except (TemplateMissing, VisionUnavailable, StepFailed) as exc:
                ok = False
                self.logger.error(f"步骤异常 {owner}.{label}({action})：{exc}")
            except Exception as exc:
                ok = False
                self.logger.error(
                    f"步骤异常 {owner}.{label}({action})：{exc}\n{traceback.format_exc()}"
                )

            if ok:
                self.report.steps_ok += 1
            else:
                self.report.steps_failed += 1
                all_ok = False

            after = step.get("after")
            if after:
                self._sleep(float(after))

            if not ok:
                policy = step.get("on_fail", "continue")
                self.logger.warn(
                    f"步骤失败 {owner}.{label}({action})，on_fail={policy}"
                )
                if policy == "abort":
                    raise AbortRun(f"{owner}.{label} 失败（on_fail=abort）")
                if policy == "next_task":
                    raise NextTask(f"{owner}.{label} 失败（on_fail=next_task）")
        return all_ok

    def _run_step(self, step: dict, owner: str, depth: int) -> bool:
        action = step.get("action")
        handler = self._handlers.get(action)
        if handler is None:
            self.logger.error(f"未知 action：{action!r}")
            return False
        return bool(handler(step, owner, depth))

    # ------------------------------------------------------------------ 基础动作
    def _do_tap(self, step, owner, depth) -> bool:
        times = int(step.get("times", 1) or 1)
        interval = float(step.get("interval", 0) or 0)
        for i in range(max(1, times)):
            if i:
                self._sleep(interval)
            x, y = step["x"], step["y"]
            coord = step.get("coord", "rel1000")
            ax, ay = self.backend.tap(x, y, coord)
            self.logger.debug(f"点击 ({x}, {y}) {coord} → 绝对 ({ax}, {ay})")
        return True

    def _do_image_tap(self, step, owner, depth) -> bool:
        if step.get("all") or step.get("index"):
            return self._do_image_tap_multi(step, owner, depth)
        times = int(step.get("times", 1) or 1)
        interval = float(step.get("interval", 0) or 0)
        offset = step.get("offset") or (0, 0)
        dx, dy = int(offset[0]), int(offset[1])
        for i in range(max(1, times)):
            if i:
                self._sleep(interval)
            match, frame = self._locate(step)
            if match is None:
                self._fail(step, f"找不到模板 {step.get('template')}（阈值 {step.get('threshold')}）", frame)
                return False
            if step.get("click", True):
                cx, cy = match.center
                ax, ay = self.backend.tap(cx + dx, cy + dy, "abs")
                self.logger.info(
                    f"识图点击 {os.path.basename(str(step.get('template')))} "
                    f"score={match.score:.3f} → ({ax}, {ay})"
                )
            else:
                self.logger.info(f"命中模板 {match}")
        return True

    def _do_image_tap_multi(self, step, owner, depth) -> bool:
        """点完所有命中（all）或只点第 n 个（index），按从上到下排序。"""
        template = self._template_path(step)
        if not os.path.isfile(template):
            raise TemplateMissing(f"模板图不存在：{template}（用任务添加器补拍）")
        offset = step.get("offset") or (0, 0)
        dx, dy = int(offset[0]), int(offset[1])
        interval = float(step.get("interval", 0) or 0)

        frame = self._grab()
        matches = self.matcher.find_all(
            frame,
            template,
            threshold=step.get("threshold"),
            max_results=max(1, int(step.get("limit", 20) or 20)),
            region=step.get("region"),
        )
        matches.sort(key=lambda m: (m.y, m.x))

        index = int(step.get("index", 0) or 0)
        if index:
            if len(matches) < index:
                self._fail(
                    step,
                    f"只找到 {len(matches)} 个 {os.path.basename(template)}，不够第 {index} 个",
                    frame,
                )
                return False
            matches = matches[index - 1 : index]
        elif step.get("limit"):
            matches = matches[: int(step["limit"])]

        if not matches:
            self._fail(
                step, f"找不到模板 {step.get('template')}（阈值 {step.get('threshold')}）", frame
            )
            return False

        name = os.path.basename(template)
        for i, match in enumerate(matches):
            self._check_stop()
            if i and interval:
                self._sleep(interval)
            cx, cy = match.center
            ax, ay = self.backend.tap(cx + dx, cy + dy, "abs")
            self.logger.info(
                f"识图点击 {name} 第 {i + 1}/{len(matches)} 个 score={match.score:.3f} → ({ax}, {ay})"
            )
        return True

    def _do_tap_list(self, step, owner, depth) -> bool:
        """按点位列表逐个点击；未勾选的点位跳过并记日志。"""
        items = step.get("items") or []
        step_id = str(step.get("id") or step.get("comment") or "tap_list")
        coord = step.get("coord", "rel1000")
        times = max(1, int(step.get("times", 1) or 1))
        interval = float(step.get("interval", 0) or 0)
        max_clicks = int(step.get("max", 0) or 0)

        if step_id in self.selection:
            chosen = {str(v) for v in self.selection[step_id]}
        elif owner and f"{owner}::{step_id}" in self.selection:
            # 界面按「任务key::步骤id」存选择，避免不同任务用同名 step id 时串台
            chosen = {str(v) for v in self.selection[f"{owner}::{step_id}"]}
        elif step.get("default_selected", True):
            chosen = {str(item.get("id")) for item in items}
        else:
            chosen = set()

        if not items:
            self.logger.warn(f"{step_id}：点位列表为空，已跳过")
            return True

        # 界面日志只看任务级结果（见 FEATURES R6），点位的勾选明细只写日志文件
        skipped = [str(item.get("id")) for item in items if str(item.get("id")) not in chosen]
        if not chosen:
            self.logger.info(f"{step_id}：跳过（未勾选任何点位）")
            return True

        clicks = 0
        for item in items:
            item_id = str(item.get("id"))
            label = item.get("name") or item_id
            if item_id not in chosen:
                self.logger.info(f"跳过未勾选 {item_id}（{label}）")
                continue
            if max_clicks and clicks >= max_clicks:
                self.logger.info(f"{step_id}：已达上限 {max_clicks} 个，停止")
                break
            self._check_stop()
            for i in range(times):
                if clicks or i:
                    self._sleep(interval)
                ax, ay = self.backend.tap(item.get("x"), item.get("y"), coord)
                clicks += 1
                suffix = f" 第 {i + 1}/{times} 次" if times > 1 else ""
                self.logger.info(f"点击 {item_id}（{label}）@ ({ax}, {ay}){suffix}")
        self.logger.info(f"{step_id}：共点击 {clicks} 个点位")
        if skipped:
            self.logger.info(f"{step_id}：跳过未勾选点位 {', '.join(skipped)}")
        return True

    def _do_swipe(self, step, owner, depth) -> bool:
        times = int(step.get("times", 1) or 1)
        interval = float(step.get("interval", 0) or 0)
        for i in range(max(1, times)):
            if i:
                self._sleep(interval)
            self.backend.swipe(
                step["x1"],
                step["y1"],
                step["x2"],
                step["y2"],
                step.get("coord", "rel1000"),
                int(step.get("duration", 300) or 300),
            )
            self.logger.debug(
                f"滑动 ({step['x1']},{step['y1']}) → ({step['x2']},{step['y2']}) "
                f"{step.get('duration', 300)}ms"
            )
        return True

    def _do_text(self, step, owner, depth) -> bool:
        value = self._expand(str(step.get("value", "")))
        if not value:
            self.logger.warn("text 内容为空，已跳过")
            return True
        times = int(step.get("times", 1) or 1)
        for _ in range(max(1, times)):
            self.backend.input_text(value)
            self.logger.info(f"输入文本：{value}")
        return True

    def _do_keyevent(self, step, owner, depth) -> bool:
        times = int(step.get("times", 1) or 1)
        interval = float(step.get("interval", 0) or 0)
        for i in range(max(1, times)):
            if i:
                self._sleep(interval)
            self.backend.key(str(step.get("key")))
            self.logger.debug(f"按键：{step.get('key')}")
        return True

    def _do_sleep(self, step, owner, depth) -> bool:
        seconds = float(step.get("seconds", 0) or 0)
        self._sleep(seconds)
        return True

    def _do_wait_image(self, step, owner, depth) -> bool:
        match, frame = self._locate(step)
        if match is None:
            self._fail(
                step,
                f"等待模板超时：{step.get('template')}（{step.get('timeout')}s）",
                frame,
            )
            return False
        self.logger.info(f"等到模板 {match}")
        return True

    def _do_wait_gone(self, step, owner, depth) -> bool:
        template = self._template_path(step)
        timeout = float(step.get("timeout", 0) or 0)
        deadline = time.monotonic() + timeout
        frame = None
        while True:
            self._check_stop()
            frame = self._grab()
            match = self.matcher.find(
                frame,
                template,
                threshold=step.get("threshold"),
                region=step.get("region"),
                multiscale=step.get("multiscale"),
                scales=step.get("scales"),
                grayscale=step.get("grayscale"),
            )
            if match is None:
                self.logger.info(f"模板已消失：{os.path.basename(str(step.get('template')))}")
                return True
            if time.monotonic() >= deadline:
                self._fail(
                    step,
                    f"等待模板消失超时：{step.get('template')}（{timeout}s，"
                    f"仍命中 score={match.score:.3f}）",
                    frame,
                )
                return False
            self._sleep(min(self.poll_interval, max(0.05, deadline - time.monotonic())))

    def _do_exists(self, step, owner, depth) -> bool:
        # timeout 缺省为 0：只截一帧，快速分支
        match, _frame = self._locate(step)
        branch = "then" if match else "else"
        steps = step.get(branch) or []
        if match:
            self.logger.info(f"命中分支 then：{match}")
        elif steps:
            self.logger.debug(f"未命中 {step.get('template')}，走 else 分支")
        if not steps:
            return True
        return self._run_steps(steps, owner, depth)

    def _do_call(self, step, owner, depth) -> bool:
        target = step.get("task")
        if not isinstance(target, str):
            self.logger.error("call 缺少 task 字段")
            return False
        if target in self._stack:
            self.logger.error(f"call 循环引用：{' → '.join(self._stack + [target])}")
            return False
        return self.run_task(target, depth=depth + 1)

    def _do_loop(self, step, owner, depth) -> bool:
        count = int(step.get("count", 1) or 1)
        steps = step.get("do") or []
        for i in range(max(1, count)):
            self.logger.debug(f"循环 {i + 1}/{count}")
            if not self._run_steps(steps, owner, depth):
                return False
        return True

    def _do_launch(self, step, owner, depth) -> bool:
        package = str(step.get("package") or "")
        self.backend.launch(package)
        self.logger.info(f"启动应用：{package}")
        return True

    def _do_kill(self, step, owner, depth) -> bool:
        package = str(step.get("package") or "")
        self.backend.kill(package)
        self.logger.info(f"关闭应用：{package}")
        return True

    # ------------------------------------------------------------------ 辅助
    def _template_path(self, step) -> str:
        rel = step.get("template")
        if not isinstance(rel, str) or not rel:
            raise TemplateMissing("step 缺少 template 字段")
        return self.taskset.template_path(rel)

    def _grab(self):
        frame = self.backend.screenshot()
        if frame is None:
            raise BackendError("截图为空")
        self._last_frame = frame
        return frame

    def _locate(self, step) -> tuple[Optional[Match], object]:
        """按 timeout 轮询等待模板出现，返回 (match, 最后一帧)。"""
        template = self._template_path(step)
        if not os.path.isfile(template):
            raise TemplateMissing(
                f"模板图不存在：{template}（用任务添加器 F3/框选补拍）"
            )
        timeout = float(step.get("timeout", 0) or 0)
        deadline = time.monotonic() + timeout
        frame = None
        while True:
            self._check_stop()
            frame = self._grab()
            match = self.matcher.find(
                frame,
                template,
                threshold=step.get("threshold"),
                region=step.get("region"),
                multiscale=step.get("multiscale"),
                scales=step.get("scales"),
                grayscale=step.get("grayscale"),
            )
            if match is not None:
                return match, frame
            if time.monotonic() >= deadline:
                return None, frame
            self._sleep(min(self.poll_interval, max(0.05, deadline - time.monotonic())))

    def _fail(self, step, message: str, frame=None) -> None:
        label = step.get("id") or step.get("comment") or step.get("action")
        self.logger.error(f"{self._current_task}.{label}：{message}")
        if frame is not None and self.screenshot_on_fail and self.logger:
            path = self.logger.save_screenshot(frame, f"fail_{self._current_task}_{label}")
            if path:
                self.logger.info(f"失败现场截图：{path}")

    def _expand(self, text: str) -> str:
        """把 ``${name}`` 替换成运行时变量（如兑换码）。"""
        if "${" not in text:
            return text

        def repl(match):
            key = match.group(1).strip()
            if key in self.variables:
                return str(self.variables[key])
            self.logger.warn(f"变量 {key} 未提供，保持原样")
            return match.group(0)

        import re

        return re.sub(r"\$\{([^}]+)\}", repl, text)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self.stop_event.wait(min(0.1, remaining)):
                raise AbortRun("收到停止指令")

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise AbortRun("收到停止指令")
