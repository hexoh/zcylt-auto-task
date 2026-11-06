"""任务计划（``task_plan.json``）：首页「任务计划」卡片的唯一数据源。

为什么要有这一份文件
--------------------
``tasks/*.json`` 是**任务定义**（步骤、模板图、坐标），改它等于改脚本逻辑，风险高；
日常真正想调的是「今天跑哪几个任务」。把这一层拆出来：

    tasks/*.json   任务怎么跑（步骤、识图、点位定义）
    task_plan.json 这次跑哪些（开关、点位、流程、变量）

界面不自己存勾选状态——它读这份 JSON。两种改法通向同一份文件：
用编辑器改 true/false（存盘后界面自动重载），或在首页点任务行右侧的开关
（走 :func:`set_task_enabled` 立刻写回）。因此「界面看到的」和「文件里写的」
永远一致，不存在两套状态谁覆盖谁的问题。

字段（全部可选，缺省见 :data:`PLAN_COMMENT`）
--------------------------------------------
``version``      固定 1
``tasks_path``   任务定义文件或目录，空 = 用 ``config.json`` 的 ``tasks_path``
``flow``         默认流程名，空 = 用 ``config.json`` 的 ``flow``
``dry_run``      演练模式，``null`` = 由界面开关决定
``tasks``        ``{"任务key": true/false}``；没写的任务沿用任务定义里的 ``enabled``。
                 首页每一行右侧的开关就是改这一项，点完立刻写回文件
``points``       ``{"任务key::步骤id": ["点位id", ...]}``，留空 = 按任务定义的 default_selected
``variables``    运行时变量（如 ``redeem_code``）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PLAN_VERSION = 1
DEFAULT_PLAN_FILE = "task_plan.json"

PLAN_COMMENT = (
    "任务计划：首页「任务计划」卡片就是读这份文件。"
    "tasks 里 true=启用、false=停止，没写的任务沿用任务定义里的 enabled；"
    "界面上的开关也是改这里（点一下立刻写回本文件）；"
    "points 控制 tap_list 点位，格式 {\"任务key::步骤id\": [\"点位id\", ...]}，"
    "留空则按任务定义的 default_selected；"
    "改完存盘，界面会自动重新载入（也可以点卡片上的「重新载入」）。"
)


@dataclass
class TaskPlan:
    """一份加载后的任务计划。加载失败不抛异常，把原因写进 ``error``。"""

    path: str = ""
    exists: bool = False
    tasks_path: str = ""
    flow: str = ""
    dry_run: Optional[bool] = None
    tasks: Dict[str, bool] = field(default_factory=dict)
    points: Dict[str, List[str]] = field(default_factory=dict)
    variables: Dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    error: str = ""
    mtime: float = 0.0

    # ------------------------------------------------------------------ 查询
    def is_enabled(self, key: str, default: bool = True) -> bool:
        """计划里显式写了就以计划为准，没写才回落到任务定义的 ``enabled``。"""
        value = self.tasks.get(key)
        return bool(default) if value is None else bool(value)

    def enabled_keys(self, taskset) -> List[str]:
        """本次要跑的任务；``watch_only`` 的（监控专用）永远不算进来。"""
        return [
            key
            for key, task in taskset.tasks.items()
            if not getattr(task, "watch_only", False)
            and self.is_enabled(key, getattr(task, "enabled", True))
        ]

    def selection(self) -> Dict[str, List[str]]:
        """界面/执行器用的点位选择（只保留非空组）。"""
        return {key: list(values) for key, values in self.points.items() if values}

    def unknown_task_keys(self, taskset) -> List[str]:
        """计划里写了、但任务定义里没有的 key（多半是任务改名了）。"""
        return [key for key in self.tasks if key not in taskset.tasks]

    def unlisted_task_keys(self, taskset) -> List[str]:
        """任务定义里有、计划里没写的 key（沿用任务自身 enabled）。"""
        return [
            key
            for key, task in taskset.tasks.items()
            if key not in self.tasks and not getattr(task, "watch_only", False)
        ]

    def point_stats(self, groups: List[dict]) -> Tuple[int, int]:
        """``(点位数, 已勾数)``，用于首页一行摘要。"""
        total = 0
        picked = 0
        for group in groups:
            items = [str(item["id"]) for item in group["items"]]
            chosen = self.chosen_points(group)
            total += len(items)
            picked += len(chosen)
        return total, picked

    def chosen_points(self, group: dict) -> List[str]:
        """某个点位组实际生效的点位 id 列表。"""
        item_ids = [str(item["id"]) for item in group["items"]]
        saved = self.points.get(group["key"])
        if saved is None:
            return item_ids if group.get("default_selected") else []
        chosen = {str(value) for value in saved}
        return [item_id for item_id in item_ids if item_id in chosen]


# ---------------------------------------------------------------------- 解析
def _as_bool(value, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if text in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    return default


def _parse_tasks(raw) -> Tuple[Dict[str, bool], str]:
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "tasks 必须是对象：{\"任务key\": true/false}"
    tasks: Dict[str, bool] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            # 兼容 {"enabled": false} 这种写法，留出以后加字段的余地
            enabled = _as_bool(value.get("enabled", True), True)
        else:
            enabled = _as_bool(value, None)
        if enabled is None:
            return tasks, f"tasks.{key} 必须是 true / false"
        tasks[str(key)] = enabled
    return tasks, ""


def _parse_points(raw) -> Tuple[Dict[str, List[str]], str]:
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "points 必须是对象：{\"任务key::步骤id\": [\"点位id\"]}"
    points: Dict[str, List[str]] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, (list, tuple)):
            return points, f"points.{key} 必须是数组"
        points[str(key)] = [str(item) for item in value]
    return points, ""


def _parse_variables(raw) -> Tuple[Dict[str, str], str]:
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "variables 必须是对象"
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}, ""


def load_plan(path: str) -> TaskPlan:
    """读取计划文件。语法错误 / 结构错误只记进 ``error``，不抛异常。"""
    plan = TaskPlan(path=os.path.abspath(path))
    if not os.path.isfile(plan.path):
        return plan

    plan.exists = True
    try:
        plan.mtime = os.path.getmtime(plan.path)
    except OSError:
        plan.mtime = 0.0

    try:
        with open(plan.path, "r", encoding="utf-8-sig") as fp:
            doc = json.load(fp)
    except json.JSONDecodeError as exc:
        plan.error = f"JSON 语法错误：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        return plan
    except OSError as exc:
        plan.error = f"读取失败：{exc}"
        return plan

    if not isinstance(doc, dict):
        plan.error = "顶层必须是一个对象：{ \"version\": 1, ... }"
        return plan

    plan.raw = doc
    plan.tasks_path = str(doc.get("tasks_path") or "").strip()
    plan.flow = str(doc.get("flow") or "").strip()
    plan.dry_run = _as_bool(doc.get("dry_run"), None)

    errors: List[str] = []
    plan.tasks, err = _parse_tasks(doc.get("tasks"))
    if err:
        errors.append(err)
    plan.points, err = _parse_points(doc.get("points"))
    if err:
        errors.append(err)
    plan.variables, err = _parse_variables(doc.get("variables"))
    if err:
        errors.append(err)
    if errors:
        plan.error = "；".join(errors)
    return plan


# ---------------------------------------------------------------------- 生成
def build_plan_document(
    taskset=None,
    *,
    flow: str = "",
    tasks_path: str = "",
    dry_run: Optional[bool] = None,
) -> dict:
    """按当前任务定义生成一份「全开」的计划，给第一次运行 / 导入模板用。"""
    tasks: Dict[str, bool] = {}
    if taskset is not None:
        for key, task in taskset.tasks.items():
            tasks[key] = bool(getattr(task, "enabled", True))
    return {
        "_comment": PLAN_COMMENT,
        "version": PLAN_VERSION,
        "tasks_path": tasks_path or "",
        "flow": flow or "",
        "dry_run": dry_run,
        "tasks": tasks,
        "points": {},
        "variables": {},
    }


def save_plan(document: dict, path: str) -> bool:
    """写回计划文件（保持中文可读、缩进统一）。

    先写同目录临时文件再 ``os.replace`` 覆盖：界面开关每点一次就写一回盘，
    写到一半被中断（或者同时被编辑器打开）也不会留下半截 JSON。
    """
    tmp_path = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(document, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def update_tasks(path: str, updates: Dict[str, bool]) -> Tuple[bool, str]:
    """一次读、一次写，把若干任务的开关写回计划文件。

    其它字段和书写顺序原样保留；返回 ``(是否成功, 失败原因)``。
    文件读不了（语法错误 / 结构不对）时**不写**——宁可这次不生效，
    也不能把用户手改到一半的计划覆盖掉。
    """
    if not updates:
        return True, ""
    if not os.path.isfile(path):
        return False, "计划文件不存在"
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            doc = json.load(fp)
    except json.JSONDecodeError as exc:
        return False, f"JSON 语法错误：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
    except OSError as exc:
        return False, f"读取失败：{exc}"

    if not isinstance(doc, dict):
        return False, "顶层必须是一个对象：{ \"version\": 1, ... }"

    tasks = doc.get("tasks")
    if tasks is None:
        tasks = {}
    elif not isinstance(tasks, dict):
        return False, "tasks 必须是对象：{\"任务key\": true/false}"
    for key, enabled in updates.items():
        if str(key):
            # 统一写成 bool，顺手把 {"enabled": false} 这种旧写法收敛掉
            tasks[str(key)] = bool(enabled)
    doc["tasks"] = tasks

    if not save_plan(doc, path):
        return False, "写入失败，检查文件是否只读或被其它程序占用"
    return True, ""


def set_task_enabled(path: str, key: str, enabled: bool) -> Tuple[bool, str]:
    """单个任务的开关（界面每一行的开关走这里）。"""
    if not key:
        return False, "任务 key 为空"
    return update_tasks(path, {str(key): bool(enabled)})


def ensure_plan(
    path: str,
    taskset=None,
    *,
    flow: str = "",
    tasks_path: str = "",
) -> TaskPlan:
    """文件不存在就按当前任务定义生成一份，再读回来。"""
    if not os.path.isfile(path) and taskset is not None:
        save_plan(
            build_plan_document(taskset, flow=flow, tasks_path=tasks_path),
            path,
        )
    return load_plan(path)
