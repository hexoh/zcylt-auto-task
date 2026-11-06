"""task_plan / features 的单元测试。

这两块都是纯逻辑（读 JSON、算开关），不碰图像库，也不需要真机。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import features  # noqa: E402
from core.task_plan import (  # noqa: E402
    build_plan_document,
    ensure_plan,
    load_plan,
    save_plan,
)
from core.task_schema import load_tasks  # noqa: E402


class _Task:
    def __init__(self, enabled=True, desc=""):
        self.enabled = enabled
        self.desc = desc


class _TaskSet:
    """只用到 enabled_keys / tasks 两个字段，手搓一个就够。"""

    def __init__(self, tasks: dict):
        self.tasks = tasks


def _write(tmp_path, payload) -> str:
    path = tmp_path / "task_plan.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------- 计划
def test_missing_plan_is_not_an_error(tmp_path):
    plan = load_plan(str(tmp_path / "task_plan.json"))
    assert plan.exists is False
    assert plan.error == ""
    assert plan.tasks == {}


def test_broken_json_reports_line_and_column(tmp_path):
    path = _write(tmp_path, '{"tasks": {"a": true,}}')
    plan = load_plan(path)
    assert plan.exists is True
    assert "JSON 语法错误" in plan.error
    assert plan.tasks == {}


def test_tasks_accept_bool_and_object_forms(tmp_path):
    path = _write(
        tmp_path,
        {"version": 1, "tasks": {"a": True, "b": False, "c": {"enabled": False}}},
    )
    plan = load_plan(path)
    assert plan.error == ""
    assert plan.tasks == {"a": True, "b": False, "c": False}


def test_unlisted_task_falls_back_to_task_definition(tmp_path):
    """计划里没写的任务，沿用 tasks/*.json 自己的 enabled，而不是一律禁用。"""
    path = _write(tmp_path, {"tasks": {"off": False}})
    plan = load_plan(path)
    taskset = _TaskSet({"off": _Task(True), "on": _Task(True), "legacy": _Task(False)})
    assert plan.enabled_keys(taskset) == ["on"]


def test_plan_overrides_task_definition(tmp_path):
    path = _write(tmp_path, {"tasks": {"taskA": True}})
    plan = load_plan(path)
    taskset = _TaskSet({"taskA": _Task(False)})
    assert plan.enabled_keys(taskset) == ["taskA"]


def test_points_default_to_default_selected(tmp_path):
    plan = load_plan(_write(tmp_path, {"tasks": {}}))
    group = {
        "key": "claim::rows",
        "items": [{"id": "r1"}, {"id": "r2"}],
        "default_selected": True,
    }
    assert plan.chosen_points(group) == ["r1", "r2"]
    group["default_selected"] = False
    assert plan.chosen_points(group) == []


def test_points_explicit_selection_filters_unknown_ids(tmp_path):
    plan = load_plan(_write(tmp_path, {"points": {"claim::rows": ["r2", "gone"]}}))
    group = {
        "key": "claim::rows",
        "items": [{"id": "r1"}, {"id": "r2"}],
        "default_selected": True,
    }
    assert plan.chosen_points(group) == ["r2"]
    assert plan.selection() == {"claim::rows": ["r2", "gone"]}


def test_unknown_task_keys_are_reported(tmp_path):
    plan = load_plan(_write(tmp_path, {"tasks": {"typoTask": True}}))
    taskset = _TaskSet({"realTask": _Task(True)})
    assert plan.unknown_task_keys(taskset) == ["typoTask"]
    assert plan.unlisted_task_keys(taskset) == ["realTask"]


def test_ensure_plan_generates_file_from_taskset(tmp_path):
    path = str(tmp_path / "task_plan.json")
    taskset = _TaskSet({"a": _Task(True, "甲"), "b": _Task(False, "乙")})
    plan = ensure_plan(path, taskset, flow="daily", tasks_path="tasks")
    assert os.path.isfile(path)
    assert plan.tasks == {"a": True, "b": False}
    assert plan.flow == "daily"
    assert plan.tasks_path == "tasks"
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["version"] == 1
    assert "_comment" in doc


def test_save_and_reload_roundtrip(tmp_path):
    path = str(tmp_path / "task_plan.json")
    doc = build_plan_document(_TaskSet({"a": _Task(True)}), flow="quick")
    assert save_plan(doc, path) is True
    plan = load_plan(path)
    assert plan.error == "" and plan.tasks == {"a": True} and plan.flow == "quick"


def test_real_taskset_can_be_enabled_end_to_end(tmp_path):
    """拿仓库里真实的 tasks/ 跑一遍：全部启用时的 key 顺序要和定义一致。"""
    taskset = load_tasks(os.path.join(ROOT, "tasks"), base_dir=ROOT)
    path = str(tmp_path / "task_plan.json")
    plan = ensure_plan(path, taskset, flow=taskset.flow_names()[0])
    assert plan.enabled_keys(taskset) == list(taskset.tasks.keys())


# ---------------------------------------------------------------------- 开关
def test_defaults_only_enable_adb():
    config = {"features": {}}
    assert features.is_enabled(config, "modes", "adb") is True
    assert features.is_enabled(config, "modes", "screen") is False
    assert [item["key"] for item in features.modes(config)] == ["adb", "screen"]


def test_visible_false_removes_entry_from_modes():
    config = {"features": {"modes": {"screen": {"visible": False}}}}
    assert [item["key"] for item in features.modes(config)] == ["adb"]


def test_resolve_mode_skips_disabled_and_falls_back():
    config = {"features": {"modes": {"adb": {"enabled": False}, "screen": {"enabled": True}}}}
    assert features.resolve_mode(config, "adb") == "screen"
    none = {"features": {"modes": {"adb": {"enabled": False}, "screen": {"enabled": False}}}}
    assert features.resolve_mode(none, "adb") is None


def test_disabled_reason_points_at_the_right_key():
    config = {"features": {"modes": {"screen": {"enabled": False}}}}
    reason = features.disabled_reason(config, "modes", "screen")
    assert "features.modes.screen.enabled" in reason
    hidden = {"features": {"modes": {"screen": {"visible": False}}}}
    assert "visible" in features.disabled_reason(hidden, "modes", "screen")
