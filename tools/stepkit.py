"""步骤合成内核：把「拾取到的坐标 / 框选的图」变成能跑的 step。

这里只放纯逻辑，不碰 Tk——这样能用 pytest 直接测，
界面（``tools/adder.py``）只负责收集输入。

输出严格遵守 FEATURES.md 8.9 的约定：

- 坐标一律 ``rel1000``
- 模板一律相对路径 ``templates/**/*.png``
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Sequence, Tuple

#: 界面上「类型」下拉的取值 → action
TYPE_ACTIONS = {
    "点击（tap）": "tap",
    "点位列表（tap_list）": "tap_list",
    "识图点击（image_tap）": "image_tap",
    "滑动（swipe）": "swipe",
    "等待图像（wait_image）": "wait_image",
    "等待消失（wait_gone）": "wait_gone",
    "条件分支（exists）": "exists",
    "输入文本（text）": "text",
    "等待（sleep）": "sleep",
    "按键（keyevent）": "keyevent",
    "调用子任务（call）": "call",
    "循环（loop）": "loop",
}

#: 需要拖拽框选的类型（框选即存模板）
IMAGE_ACTIONS = {"image_tap", "wait_image", "wait_gone", "exists"}

#: 需要一次性点两下的类型
TWO_POINT_ACTIONS = {"swipe"}

#: 连续点、攒成一个步骤的类型
LIST_ACTIONS = {"tap_list"}

#: 完全不拾取、纯手填的类型
MANUAL_ACTIONS = {"text", "sleep", "keyevent", "call", "loop"}

#: 模板保存的子目录（按类型分组，交付时一眼能看出用途）
TEMPLATE_DIRS = {
    "image_tap": "ui",
    "wait_image": "ui",
    "wait_gone": "ui",
    "exists": "ui",
}

BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def action_of(type_label: str) -> str:
    """下拉文字 → action；也接受直接传 action。"""
    if type_label in TYPE_ACTIONS:
        return TYPE_ACTIONS[type_label]
    value = str(type_label).strip()
    if value in TYPE_ACTIONS.values():
        return value
    raise KeyError(f"未知步骤类型：{type_label}")


def type_label_of(action: str) -> str:
    for label, act in TYPE_ACTIONS.items():
        if act == action:
            return label
    return action


def needs_pick(action: str) -> bool:
    return action not in MANUAL_ACTIONS


def is_list_action(action: str) -> bool:
    return action in LIST_ACTIONS


def is_image_action(action: str) -> bool:
    return action in IMAGE_ACTIONS


def is_two_point_action(action: str) -> bool:
    return action in TWO_POINT_ACTIONS


# ---------------------------------------------------------------------- 命名
def auto_key(desc: str, existing: Iterable[str] = ()) -> str:
    """从显示名生成任务 key（英数驼峰）；中文名走 ``taskN`` 兜底。

    规则：只取 ASCII 字母数字，按空格/下划线/短横分组转驼峰；
    纯中文（或结果为空）时用 ``task1``、``task2``……
    """
    words = re.findall(r"[A-Za-z0-9]+", str(desc or ""))
    taken = {str(k) for k in existing}
    if not words:
        idx = 1
        while f"task{idx}" in taken:
            idx += 1
        return f"task{idx}"

    head, *rest = words
    key = head[:1].lower() + head[1:]
    for word in rest:
        key += word[:1].upper() + word[1:]
    if key and key[0].isdigit():
        key = "task" + key
    if key in taken:
        idx = 2
        while f"{key}{idx}" in taken:
            idx += 1
        key = f"{key}{idx}"
    return key


def safe_name(name: str) -> str:
    """把用户输入的文件名/模板名洗成安全路径片段。"""
    text = str(name or "").strip().replace("\\", "/")
    if text.lower().startswith("templates/"):
        text = text[len("templates/") :]
    if text.lower().endswith(".png"):
        text = text[:-4]
    parts = []
    for part in text.split("/"):
        part = BAD_CHARS.sub("_", part).strip("._")
        if part:
            parts.append(part)
    return "/".join(parts) or "template"


def template_rel_path(task_key: str, index: int, action: str, name: str = "") -> str:
    """模板落盘位置：``templates/<ui|auto>/<key>_<序号>[_<名字>].png``。

    按类型分目录（识图类都进 ``ui``），文件名带任务 key 和序号，
    多人交付同一份 ``templates/`` 时不会互相覆盖。
    """
    folder = TEMPLATE_DIRS.get(action, "auto")
    key = safe_name(task_key or "task").replace("/", "_")
    stem = f"{key}_{max(0, int(index)):02d}"
    if name:
        cleaned = safe_name(name).replace("/", "_")
        if cleaned and cleaned != "template":
            stem += f"_{cleaned}"
    return f"templates/{folder}/{stem}.png"


# ---------------------------------------------------------------------- 生成
def _base(action: str, **fields) -> dict:
    step = {"action": action}
    step.update({k: v for k, v in fields.items() if v is not None})
    return step


def make_tap(
    x: int,
    y: int,
    times: int = 1,
    interval: float = 0.8,
    id: str = "",
    comment: str = "",
) -> dict:
    step = _base("tap", x=int(x), y=int(y), coord="rel1000", id=id or None, comment=comment or None)
    if times and int(times) != 1:
        step["times"] = int(times)
    if interval is not None:
        step["interval"] = float(interval)
    return step


def make_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: int = 400,
    id: str = "",
    comment: str = "",
) -> dict:
    return _base(
        "swipe",
        x1=int(x1),
        y1=int(y1),
        x2=int(x2),
        y2=int(y2),
        coord="rel1000",
        duration=int(duration),
        id=id or None,
        comment=comment or None,
    )


def make_image_step(
    action: str,
    template: str,
    threshold: float = 0.85,
    timeout: Optional[float] = None,
    id: str = "",
    comment: str = "",
) -> dict:
    """识图类步骤：image_tap / wait_image / wait_gone / exists。"""
    if action not in IMAGE_ACTIONS:
        raise ValueError(f"{action} 不是识图类步骤")
    step = {
        "action": action,
        "template": template,
        "id": id or None,
        "comment": comment or None,
    }
    if action == "image_tap":
        step["threshold"] = float(threshold)
    else:
        step["threshold"] = float(threshold)
        step["timeout"] = float(timeout if timeout is not None else 10)
    if action == "exists":
        step["then"] = []
        step["else"] = []
    return {k: v for k, v in step.items() if v is not None}


def make_items(points: Sequence[Tuple[int, int]], names: Optional[Sequence[str]] = None) -> List[dict]:
    """``[(x, y), ...]`` → ``tap_list`` 的 ``items``（每个点位可单独勾选）。"""
    names = list(names or [])
    items = []
    for i, (x, y) in enumerate(points):
        label = names[i] if i < len(names) and names[i] else f"第 {i + 1} 个"
        items.append({"id": f"p{i + 1}", "name": label, "x": int(x), "y": int(y)})
    return items


def make_tap_list(
    points: Sequence[Tuple[int, int]],
    step_id: str = "rows",
    names: Optional[Sequence[str]] = None,
    interval: float = 0.8,
    default_selected: bool = True,
) -> dict:
    if not points:
        raise ValueError("点位列表至少要有一个点")
    return {
        "action": "tap_list",
        "id": step_id or "rows",
        "coord": "rel1000",
        "default_selected": bool(default_selected),
        "interval": float(interval),
        "comment": "点位列表：界面上按需勾选",
        "items": make_items(points, names),
    }


def unique_step_id(base: str, steps: Sequence[dict]) -> str:
    """同一个任务里可能有多个点位列表，步骤 id 必须唯一（界面靠它分组勾选）。"""
    base = (base or "rows").strip() or "rows"
    used = {str(step.get("id")) for step in steps or [] if step.get("id")}
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def make_sleep(seconds: float = 1.0, comment: str = "") -> dict:
    return _base("sleep", seconds=float(seconds), comment=comment or None)

def make_text(value: str, comment: str = "") -> dict:
    return _base("text", value=str(value), comment=comment or None)


def make_key(key: str, comment: str = "") -> dict:
    return _base("keyevent", key=str(key), comment=comment or None)


def make_call(task: str, comment: str = "") -> dict:
    return _base("call", task=str(task), comment=comment or None)


def make_loop(count: int, do: Optional[List[dict]] = None, comment: str = "") -> dict:
    return _base("loop", count=int(count), do=list(do or []), comment=comment or None)


# ---------------------------------------------------------------------- 摘要
def describe_step(step: dict) -> Tuple[str, str]:
    """返回 ``(类型标签, 关键参数摘要)``，列表里一行显示一步。"""
    action = str(step.get("action") or "?")
    label = type_label_of(action)
    if action == "tap":
        text = f"({step.get('x')}, {step.get('y')})"
        if int(step.get("times") or 1) > 1:
            text += f" ×{step['times']}"
        return label, text
    if action == "swipe":
        return label, f"({step.get('x1')}, {step.get('y1')}) → ({step.get('x2')}, {step.get('y2')})"
    if action == "tap_list":
        items = step.get("items") or []
        preview = "、".join(str(i.get("name") or i.get("id")) for i in items[:3])
        more = f" 等 {len(items)} 个" if len(items) > 3 else f"（{len(items)} 个）"
        return label, f"{preview}{more if preview else ''}".strip()
    if action in IMAGE_ACTIONS:
        return label, os.path.basename(str(step.get("template") or ""))
    if action == "sleep":
        return label, f"{step.get('seconds')}s"
    if action == "text":
        return label, str(step.get("value") or "")
    if action == "keyevent":
        return label, str(step.get("key") or "")
    if action == "call":
        return label, str(step.get("task") or "")
    if action == "loop":
        return label, f"×{step.get('count')}（{len(step.get('do') or [])} 步）"
    return label, ""


def step_file_note(step: dict) -> str:
    """给步骤补一句人工备注（保存时写进 JSON，方便别人读）。"""
    if step.get("comment"):
        return str(step["comment"])
    label, detail = describe_step(step)
    return f"{label} {detail}".strip()


def summarize_steps(steps: Sequence[dict]) -> str:
    """一行话概括整个任务，用于状态栏。"""
    if not steps:
        return "还没有步骤"
    counts = {}
    for step in steps:
        action = str(step.get("action") or "?")
        counts[action] = counts.get(action, 0) + 1
    parts = [f"{type_label_of(k)}×{v}" for k, v in counts.items()]
    return f"{len(steps)} 步：" + "，".join(parts)
