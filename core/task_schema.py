"""任务 JSON 的加载与校验。

一套 JSON 跑双后端：这里负责把 ``tasks/*.json`` 解析成 :class:`TaskSet`，
在加载阶段就把「未知 action / 缺字段 / call 打转 / 模板图不存在 / 坐标越界」
这些问题全部指出来，避免跑到一半才炸。

结构：``tasks`` 按 key 定义原子任务，``flows`` 按 key 组合执行顺序。
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .coords import COORDS, normalize_coord
from .errors import TaskSchemaError

#: 全部 action
ACTIONS = {
    "tap",
    "image_tap",
    "tap_list",
    "swipe",
    "text",
    "keyevent",
    "sleep",
    "wait_image",
    "wait_gone",
    "exists",
    "call",
    "loop",
    "launch",
    "kill",
}

#: 需要识图的 action
IMAGE_ACTIONS = {"image_tap", "wait_image", "wait_gone", "exists"}

#: 带坐标的 action
COORD_ACTIONS = {"tap", "swipe"}

#: 失败策略
ON_FAIL = {"continue", "abort", "next_task"}

#: 缺省值（可被 JSON 顶层 defaults 覆盖）
DEFAULT_DEFAULTS = {"interval": 1.0, "timeout": 10, "threshold": 0.85, "after": 0.5}

#: step 允许出现的字段（多余字段打 warning，抓拼写错误）
ALLOWED_STEP_KEYS = {
    "id",
    "comment",
    "desc",
    "action",
    "times",
    "interval",
    "timeout",
    "on_fail",
    "after",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "coord",
    "duration",
    "template",
    "threshold",
    "offset",
    "click",
    "value",
    "key",
    "seconds",
    "then",
    "else",
    "task",
    "count",
    "do",
    "package",
    "multiscale",
    "scales",
    "grayscale",
    "region",
    # tap_list（点位列表）
    "items",
    "default_selected",
    "max",
    # image_tap 多目标
    "all",
    "limit",
    "index",
}

REQUIRED_FIELDS = {
    "tap": ("x", "y"),
    "image_tap": ("template",),
    "tap_list": ("items",),
    "swipe": ("x1", "y1", "x2", "y2"),
    "text": ("value",),
    "keyevent": ("key",),
    "sleep": ("seconds",),
    "wait_image": ("template",),
    "wait_gone": ("template",),
    "exists": ("template",),
    "call": ("task",),
    "loop": ("count", "do"),
    "launch": ("package",),
    "kill": ("package",),
}


@dataclass
class TaskDef:
    key: str
    desc: str = ""
    steps: List[dict] = field(default_factory=list)
    enabled: bool = True
    source: str = ""
    #: 声明的总步骤数（顶层步骤）；0 = 未声明 = 不限，步数即 ``len(steps)``
    step_count: int = 0
    #: 只给循环监控用：不进流程、不算进主页任务开关，只由 watcher 显式调用
    watch_only: bool = False

    @property
    def title(self) -> str:
        return self.desc or self.key

    @property
    def declared_steps(self) -> int:
        """声明值；未声明时回落到实际步数（UI 只关心"到底几步"）。"""
        return self.step_count or len(self.steps)

    @property
    def steps_mismatch(self) -> bool:
        """声明了总步数却和实际步数对不上。"""
        return bool(self.step_count) and self.step_count != len(self.steps)


@dataclass
class TaskSet:
    tasks: Dict[str, TaskDef] = field(default_factory=dict)
    flows: Dict[str, List[str]] = field(default_factory=dict)
    #: 流程的中文显示名：{"daily": "每日任务"}；没有就回落到 key
    flow_labels: Dict[str, str] = field(default_factory=dict)
    defaults: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_DEFAULTS))
    base_dir: str = ""
    source: str = ""
    warnings: List[str] = field(default_factory=list)
    missing_templates: List[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 查询
    def task_keys(self) -> List[str]:
        return list(self.tasks.keys())

    def flow_names(self) -> List[str]:
        return list(self.flows.keys())

    def watch_task_keys(self) -> List[str]:
        """只由循环监控调用的任务（``watch_only: true``）。"""
        return [key for key, task in self.tasks.items() if getattr(task, "watch_only", False)]

    def flow_label(self, name: str) -> str:
        """流程的显示名（中文），没配就回落到 key —— 界面只显示这个。"""
        return self.flow_labels.get(name) or name

    def enabled_tasks(self) -> List[TaskDef]:
        return [t for t in self.tasks.values() if t.enabled]

    def step_count(self) -> int:
        def count(steps: Sequence[dict]) -> int:
            total = 0
            for step in steps:
                total += 1
                total += count(step.get("then") or [])
                total += count(step.get("else") or [])
                total += count(step.get("do") or [])
            return total

        return sum(count(t.steps) for t in self.tasks.values())

    def resolve_flow(self, name: str, enabled: Optional[Iterable[str]] = None) -> List[str]:
        """把 flow 名解析成有序任务 key 列表；也允许直接传任务名。"""
        if name in self.flows:
            keys = list(self.flows[name])
        elif name in self.tasks:
            keys = [name]
        else:
            raise KeyError(f"未知流程：{name}（可用：{', '.join(self.flow_names()) or '无'}）")
        if enabled is not None:
            allow: Set[str] = set(enabled)
            keys = [k for k in keys if k in allow]
        return keys

    def template_path(self, rel: str) -> str:
        return os.path.normpath(os.path.join(self.base_dir, rel))

    # ---------------------------------------------------------------- 点位
    def point_groups(self) -> List[dict]:
        """汇总所有 ``tap_list`` 步骤，供界面渲染「点位勾选」。

        一个 ``tap_list`` 步骤就是一个可勾选的分组（例如「领邮件奖励」下面 20 个
        奖励点位）。返回的 ``key`` 是 ``任务key::步骤id``，执行器的 ``selection``
        就按这个键取值——同一个步骤 id 出现在两个任务里也不会串。

        每项字段：
            task / task_title   任务 key 与显示名
            step_id / step_name 步骤 id 与说明（comment）
            key                 选择键 ``task::step_id``
            items               点位数组（已归一化，含 id / name / x / y）
            default_selected    未显式勾选时默认是否全选
        """
        groups: List[dict] = []

        def walk(task: TaskDef, steps: Sequence[dict]) -> None:
            for step in steps or []:
                if step.get("action") == "tap_list" and step.get("items"):
                    step_id = str(step.get("id") or step.get("comment") or "tap_list")
                    groups.append(
                        {
                            "task": task.key,
                            "task_title": task.title,
                            "step_id": step_id,
                            "step_name": str(step.get("comment") or ""),
                            "key": f"{task.key}::{step_id}",
                            "items": list(step["items"]),
                            "default_selected": bool(step.get("default_selected", True)),
                        }
                    )
                for branch in ("then", "else", "do"):
                    walk(task, step.get(branch) or [])

        for task in self.tasks.values():
            if task.enabled:
                walk(task, task.steps)
        return groups


def _derive_base_dir(path: str) -> str:
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return os.path.dirname(path)
    parent = os.path.dirname(path)
    if os.path.basename(parent) == "tasks":
        return os.path.dirname(parent)
    return parent


def _collect_files(path: str) -> List[str]:
    path = os.path.abspath(path)
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
        if not files:
            raise TaskSchemaError([f"目录下没有 .json 任务文件：{path}"], source=path)
        return files
    if not os.path.isfile(path):
        raise TaskSchemaError([f"任务文件不存在：{path}"], source=path)
    return [path]


class _Validator:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.missing: List[str] = []
        self.call_edges: Dict[str, Set[str]] = {}

    # ---------------------------------------------------------------- 工具
    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def _check_template(self, rel: str, where: str) -> None:
        if not isinstance(rel, str) or not rel.strip():
            self.err(f"{where}: template 必须是非空字符串")
            return
        full = os.path.normpath(os.path.join(self.base_dir, rel))
        if not os.path.isfile(full):
            if rel not in self.missing:
                self.missing.append(rel)
            self.warn(f"{where}: 模板图不存在 → {rel}（用任务添加器补拍）")

    # ---------------------------------------------------------------- 校验
    def _validate_items(self, items, where: str, coord: str = "rel1000") -> List[dict]:
        """校验 tap_list 的 items：id 唯一、坐标是数字。"""
        if not isinstance(items, list) or not items:
            self.err(f"{where}: items 必须是非空数组（每个元素是一个点位）")
            return []
        seen = set()
        result: List[dict] = []
        for i, raw in enumerate(items):
            here = f"{where}.items[{i}]"
            if not isinstance(raw, dict):
                self.err(f"{here}: 点位必须是对象")
                continue
            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                self.err(f"{here}: 缺少 id（点位标识，界面勾选和日志都要用）")
                item_id = f"item{i + 1}"
            elif item_id in seen:
                self.err(f"{here}: 点位 id 重复：{item_id}")
            seen.add(item_id)

            item = dict(raw)
            item["id"] = item_id
            item.setdefault("name", item_id)
            for key in ("x", "y"):
                if not _is_number(item.get(key)):
                    self.err(f"{here}: {key} 必须是数字，收到 {item.get(key)!r}")
                elif coord == "rel1000" and not (0 <= float(item[key]) <= 1000):
                    self.warn(f"{here}: {key}={item[key]} 超出 rel1000 的 0-1000 范围")
            if not isinstance(item.get("name"), str):
                item["name"] = str(item["name"])
            result.append(item)
        return result

    def validate_steps(
        self,
        steps,
        where: str,
        defaults: dict,
        owner: Optional[str] = None,
        depth: int = 0,
    ) -> List[dict]:
        if depth > 12:
            self.err(f"{where}: 嵌套层级过深")
            return []
        if not isinstance(steps, list):
            self.err(f"{where}: 必须是一个数组（steps）")
            return []

        normalized: List[dict] = []
        for idx, raw in enumerate(steps):
            here = f"{where}[{idx}]"
            if not isinstance(raw, dict):
                self.err(f"{here}: step 必须是对象")
                continue

            step = dict(raw)
            action = step.get("action")
            if not action:
                self.err(f"{here}: 缺少 action")
                continue
            if action not in ACTIONS:
                self.err(f"{here}: 未知 action {action!r}（可用：{', '.join(sorted(ACTIONS))}）")
                continue
            here = f"{where}[{idx}:{action}]"

            unknown = set(step) - ALLOWED_STEP_KEYS
            if unknown:
                self.warn(f"{here}: 未识别字段 {', '.join(sorted(unknown))}（是否拼写错误？）")

            # 必填字段
            for key in REQUIRED_FIELDS.get(action, ()):
                if key not in step:
                    self.err(f"{here}: 缺少字段 {key}")

            # 失败策略
            if "on_fail" in step and step["on_fail"] not in ON_FAIL:
                self.err(f"{here}: on_fail 只能是 {', '.join(sorted(ON_FAIL))}")

            # 坐标
            if action in COORD_ACTIONS:
                try:
                    step["coord"] = normalize_coord(step.get("coord"))
                except ValueError as exc:
                    self.err(f"{here}: {exc}")
                for key in REQUIRED_FIELDS[action]:
                    if key in step and not _is_number(step[key]):
                        self.err(f"{here}: {key} 必须是数字，收到 {step[key]!r}")
                if step.get("coord") == "rel1000":
                    for key in REQUIRED_FIELDS[action]:
                        if _is_number(step.get(key)) and not (0 <= float(step[key]) <= 1000):
                            self.warn(f"{here}: {key}={step[key]} 超出 rel1000 的 0-1000 范围")

            # 识图
            if action in IMAGE_ACTIONS:
                self._check_template(step.get("template"), here)
                for key, default in (
                    ("threshold", defaults.get("threshold", 0.85)),
                    ("timeout", defaults.get("timeout", 10)),
                ):
                    step.setdefault(key, default)
                if not _is_number(step.get("threshold")):
                    self.err(f"{here}: threshold 必须是数字")
                if not _is_number(step.get("timeout")):
                    self.err(f"{here}: timeout 必须是数字")
                if "offset" in step:
                    off = step["offset"]
                    if not (isinstance(off, (list, tuple)) and len(off) == 2 and all(_is_number(v) for v in off)):
                        self.err(f"{here}: offset 必须是 [dx, dy]")
                if "region" in step:
                    reg = step["region"]
                    if not (isinstance(reg, (list, tuple)) and len(reg) == 4 and all(_is_number(v) for v in reg)):
                        self.err(f"{here}: region 必须是 [x, y, w, h]")

            if action in ("tap", "image_tap", "swipe", "text"):
                step.setdefault("interval", defaults.get("interval", 1.0))
                if "times" in step and (not _is_number(step["times"]) or int(step["times"]) < 1):
                    self.err(f"{here}: times 必须是 >=1 的整数")

            if action == "image_tap" and (step.get("all") or step.get("index")):
                if "all" in step and not isinstance(step["all"], bool):
                    self.err(f"{here}: all 必须是 true/false")
                if "limit" in step and (
                    not _is_number(step["limit"]) or int(step["limit"]) < 1
                ):
                    self.err(f"{here}: limit 必须是 >=1 的整数")
                if "index" in step and (
                    not _is_number(step["index"]) or int(step["index"]) < 1
                ):
                    self.err(f"{here}: index 必须是从 1 开始的整数")

            if action == "tap_list":
                step["coord"] = normalize_coord(step.get("coord"))
                step.setdefault("interval", defaults.get("interval", 1.0))
                step.setdefault("default_selected", True)
                if not isinstance(step.get("default_selected"), bool):
                    self.err(f"{here}: default_selected 必须是 true/false")
                if "max" in step and (not _is_number(step["max"]) or int(step["max"]) < 1):
                    self.err(f"{here}: max 必须是 >=1 的整数")
                step["items"] = self._validate_items(step.get("items"), here, step["coord"])

            if action == "swipe":
                step.setdefault("duration", 300)
                if not _is_number(step.get("duration")):
                    self.err(f"{here}: duration 必须是毫秒数")

            if action == "sleep" and "seconds" in step and not _is_number(step["seconds"]):
                self.err(f"{here}: seconds 必须是数字")

            if action == "text" and not isinstance(step.get("value"), str):
                if _is_number(step.get("value")):
                    step["value"] = str(step["value"])
                else:
                    self.err(f"{here}: value 必须是字符串")

            if action == "call":
                target = step.get("task")
                if not isinstance(target, str):
                    self.err(f"{here}: task 必须是任务 key")
                elif owner is not None:
                    self.call_edges.setdefault(owner, set()).add(target)

            if action == "loop":
                if not _is_number(step.get("count")) or int(step.get("count", 0)) < 1:
                    self.err(f"{here}: count 必须是 >=1 的整数")
                step["do"] = self.validate_steps(
                    step.get("do"), here + ".do", defaults, owner, depth + 1
                )

            if action == "exists":
                step["then"] = self.validate_steps(
                    step.get("then") or [], here + ".then", defaults, owner, depth + 1
                )
                step["else"] = self.validate_steps(
                    step.get("else") or [], here + ".else", defaults, owner, depth + 1
                )

            step.setdefault("after", defaults.get("after", 0.5))
            normalized.append(step)

        return normalized

    # ---------------------------------------------------------------- 环检测
    def check_cycles(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {key: WHITE for key in self.call_edges}

        def dfs(node: str, stack: List[str]) -> None:
            color[node] = GRAY
            stack.append(node)
            for nxt in self.call_edges.get(node, ()):  # noqa: B007
                if nxt not in color:
                    continue  # 引用不存在，前面已报错
                if color[nxt] == GRAY:
                    cycle = " → ".join(stack[stack.index(nxt) :] + [nxt])
                    self.err(f"call 存在循环引用：{cycle}")
                elif color[nxt] == WHITE:
                    dfs(nxt, stack)
            stack.pop()
            color[node] = BLACK

        for node in list(color):
            if color[node] == WHITE:
                dfs(node, [])


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_step_count(value, validator: "_Validator", where: str) -> int:
    """解析任务级 ``step_count``（声明的总步骤数，顶层计数，不含 loop/exists 内部）。

    返回 0 表示未声明（不限步数）。非法值记错误，不影响其他字段的校验。
    """
    if value is None:
        return 0
    if not _is_number(value) or int(value) < 1:
        validator.err(f"{where}: step_count 必须是 >=1 的整数（不填 = 不限步数）")
        return 0
    if int(value) != float(value):
        validator.err(f"{where}: step_count 必须是整数，收到 {value!r}")
        return 0
    return int(value)


def _parse_document(
    doc: dict,
    source: str,
    validator: _Validator,
    tasks: Dict[str, TaskDef],
    flows: Dict[str, List[str]],
    flow_labels: Dict[str, str],
    defaults: Dict[str, float],
    warnings: List[str],
) -> None:
    if not isinstance(doc, dict):
        validator.err(f"{source}: 顶层必须是对象")
        return

    version = doc.get("version", 1)
    if version != 1:
        validator.warn(f"{source}: version={version}，当前只支持 version=1")

    file_defaults = dict(DEFAULT_DEFAULTS)
    file_defaults.update(defaults)
    raw_defaults = doc.get("defaults") or {}
    if raw_defaults:
        if not isinstance(raw_defaults, dict):
            validator.err(f"{source}: defaults 必须是对象")
        else:
            for key, value in raw_defaults.items():
                if key not in DEFAULT_DEFAULTS:
                    validator.warn(f"{source}: defaults 里的 {key!r} 不是已知参数，已忽略")
                    continue
                if not _is_number(value):
                    validator.err(f"{source}: defaults.{key} 必须是数字")
                    continue
                file_defaults[key] = value

    raw_tasks = doc.get("tasks") or {}
    if not isinstance(raw_tasks, dict):
        validator.err(f"{source}: tasks 必须是对象（按 key 定义）")
        raw_tasks = {}

    skipped_enabled = False
    for key, body in raw_tasks.items():
        if not isinstance(body, dict):
            validator.err(f"{source}: tasks.{key} 必须是对象")
            continue
        if key in tasks:
            validator.err(f"{source}: 任务 key 重复：{key}（已在 {tasks[key].source} 中定义）")
            continue
        enabled = bool(body.get("enabled", True))
        if not enabled:
            skipped_enabled = True
        where = f"{os.path.basename(source)}:{key}"
        steps = validator.validate_steps(
            body.get("steps"), where, file_defaults, owner=key
        )
        declared = _read_step_count(body.get("step_count"), validator, where)
        if declared and declared != len(steps):
            # 声明值是给人看的一致性约束，改了步骤忘了改数字不该让整份配置跑不起来
            validator.warn(
                f"{where}: 声明的总步数是 {declared}，实际顶层步骤 {len(steps)} 个"
                "（step_count 与 steps 不一致）"
            )
        tasks[key] = TaskDef(
            key=key,
            desc=str(body.get("desc") or body.get("name") or ""),
            steps=steps,
            enabled=enabled,
            source=source,
            step_count=declared,
            watch_only=bool(body.get("watch_only", False)),
        )

    raw_flows = doc.get("flows") or {}
    if not isinstance(raw_flows, dict):
        validator.err(f"{source}: flows 必须是对象（按 key 定义数组）")
        raw_flows = {}
    for name, keys in raw_flows.items():
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            validator.err(f"{source}: flows.{name} 必须是任务 key 数组")
            continue
        if name in flows:
            validator.err(f"{source}: 流程名重复：{name}")
            continue
        flows[name] = list(keys)

    # 流程中文名：只影响界面显示，多个文件可以各自补充，不冲突
    raw_labels = doc.get("flow_labels") or {}
    if not isinstance(raw_labels, dict):
        validator.warn(f"{source}: flow_labels 必须是对象（流程名 → 中文名）")
    else:
        for name, label in raw_labels.items():
            if not isinstance(label, str) or not label.strip():
                validator.warn(f"{source}: flow_labels.{name} 必须是非空字符串，已忽略")
                continue
            flow_labels[str(name)] = label.strip()

    defaults.update(file_defaults)
    if skipped_enabled:
        warnings.append(f"{source}: 存在 enabled=false 的任务，运行时会被跳过")


def load_tasks(
    path: str,
    base_dir: Optional[str] = None,
    logger=None,
) -> TaskSet:
    """加载并校验任务配置。校验失败抛 :class:`TaskSchemaError`。"""
    files = _collect_files(path)
    base = os.path.abspath(base_dir) if base_dir else _derive_base_dir(path)

    validator = _Validator(base)
    tasks: Dict[str, TaskDef] = {}
    flows: Dict[str, List[str]] = {}
    flow_labels: Dict[str, str] = {}
    defaults: Dict[str, float] = dict(DEFAULT_DEFAULTS)
    extra_warnings: List[str] = []

    for file in files:
        try:
            with open(file, "r", encoding="utf-8") as fp:
                doc = json.load(fp)
        except json.JSONDecodeError as exc:
            raise TaskSchemaError(
                [f"{os.path.basename(file)}: JSON 语法错误 第{exc.lineno}行第{exc.colno}列：{exc.msg}"],
                source=file,
            )
        except OSError as exc:
            raise TaskSchemaError([f"读取失败：{exc}"], source=file)

        _parse_document(
            doc, file, validator, tasks, flows, flow_labels, defaults, extra_warnings
        )

    # 流程引用检查
    for name, keys in flows.items():
        for key in keys:
            if key not in tasks:
                validator.err(f"flows.{name}: 引用了不存在的任务 {key!r}")

    validator.check_cycles()

    if validator.errors:
        raise TaskSchemaError(validator.errors, validator.warnings, source=path)

    taskset = TaskSet(
        tasks=tasks,
        flows=flows,
        flow_labels=flow_labels,
        defaults=defaults,
        base_dir=base,
        source=os.path.abspath(path),
        warnings=validator.warnings + extra_warnings,
        missing_templates=validator.missing,
    )

    if logger:
        logger.info(
            f"任务配置已加载：{len(tasks)} 个任务 / {len(flows)} 个流程 / "
            f"{taskset.step_count()} 个步骤（{base}）"
        )
        for warning in taskset.warnings:
            logger.warn(warning)
        if taskset.missing_templates:
            logger.warn(
                f"共 {len(taskset.missing_templates)} 张模板图缺失，相关步骤会失败；"
                "请用「任务添加器」补拍"
            )
    return taskset


def save_tasks_document(path: str, doc: dict) -> None:
    """回写任务 JSON（保留中文和缩进）。"""
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(doc, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


# ---------------------------------------------------------------------- 单任务校验
@dataclass
class TaskCheck:
    """单个任务的校验结果（给「任务 JSON 构建器」用，不依赖整份配置）。"""

    key: str
    desc: str = ""
    steps: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_templates: List[str] = field(default_factory=list)
    #: 声明的总步骤数（0 = 不限）
    declared_steps: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return f"✘ {len(self.errors)} 个错误：" + "；".join(self.errors[:5])
        text = f"✔ 校验通过：{len(self.steps)} 个顶层步骤"
        if self.declared_steps:
            text += "（与声明的总步数一致）"
        if self.warnings:
            text += f"，{len(self.warnings)} 条提醒"
        if self.missing_templates:
            text += f"，缺 {len(self.missing_templates)} 张模板图"
        return text


def validate_task(
    key: str,
    body: dict,
    base_dir: str,
    defaults: Optional[dict] = None,
) -> TaskCheck:
    """只校验一个任务定义（构建器边写边查用）。

    ``call`` 引用的目标不在本次校验范围内（构建时可能还没写完），
    因此不做存在性检查，交给整份配置的 :func:`load_tasks` 兜底。
    """
    if not isinstance(key, str) or not key.strip():
        return TaskCheck(key=str(key), errors=["任务 key 不能为空"])
    if not isinstance(body, dict):
        return TaskCheck(key=key, errors=["任务定义必须是对象，如 {\"desc\": \"...\", \"steps\": []}"])

    validator = _Validator(os.path.abspath(base_dir))
    merged = dict(DEFAULT_DEFAULTS)
    merged.update(defaults or {})
    steps = validator.validate_steps(body.get("steps"), f"{key}", merged, owner=key)

    declared = _read_step_count(body.get("step_count"), validator, key)
    if declared and declared != len(steps):
        # 这里是"保存前"的硬校验：声明了 N 步就得正好 N 步，否则存进去会自相矛盾
        validator.err(
            f"声明的总步数是 {declared}，当前实际 {len(steps)} 步："
            f"请补齐/删掉步骤，或把总步数改成 {len(steps)}（留空 = 不限）"
        )

    return TaskCheck(
        key=key,
        desc=str(body.get("desc") or body.get("name") or ""),
        steps=steps,
        errors=validator.errors,
        warnings=validator.warnings,
        missing_templates=validator.missing,
        declared_steps=declared,
    )


def upsert_task_document(doc: Optional[dict], key: str, body: dict, flow: str = "") -> dict:
    """把任务写进一份任务文档（保留同文件里的其他任务/流程）。"""
    result = dict(doc or {})
    result.setdefault("version", 1)
    tasks = result.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    tasks[key] = body
    result["tasks"] = tasks

    if flow:
        flows = result.get("flows")
        if not isinstance(flows, dict):
            flows = {}
        keys = [k for k in (flows.get(flow) or []) if isinstance(k, str)]
        if key not in keys:
            keys.append(key)
        flows[flow] = keys
        result["flows"] = flows
    return result


def task_files(path: str) -> List[str]:
    """列出某个目录（或单个文件）下的任务 JSON。"""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "*.json")))
    return [path] if os.path.isfile(path) else []
