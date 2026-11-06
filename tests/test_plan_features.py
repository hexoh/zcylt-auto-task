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
    set_task_enabled,
    update_tasks,
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


# ---------------------------------------------------------------- 界面开关回写
def test_set_task_enabled_writes_back_and_keeps_other_fields(tmp_path):
    path = _write(
        tmp_path,
        {"version": 1, "flow": "daily", "tasks": {"a": True}, "points": {"a::s": ["p1"]}},
    )
    ok, message = set_task_enabled(path, "a", False)
    assert ok is True and message == ""

    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["tasks"]["a"] is False
    # 其它字段原样保留，不能被开关顺手抹掉
    assert doc["flow"] == "daily"
    assert doc["points"] == {"a::s": ["p1"]}
    assert load_plan(path).is_enabled("a") is False


def test_set_task_enabled_adds_missing_key(tmp_path):
    path = _write(tmp_path, {"tasks": {"a": True}})
    assert set_task_enabled(path, "b", False)[0] is True
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["tasks"] == {"a": True, "b": False}


def test_set_task_enabled_flattens_object_form(tmp_path):
    """{"enabled": false} 这种旧写法，被开关点过之后收敛成纯 bool。"""
    path = _write(tmp_path, {"tasks": {"a": {"enabled": False}}})
    assert set_task_enabled(path, "a", True)[0] is True
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["tasks"]["a"] is True
    assert load_plan(path).is_enabled("a") is True


def test_set_task_enabled_keeps_broken_json_untouched(tmp_path):
    """文件语法错时宁可不写，也不能把用户手改到一半的内容覆盖掉。"""
    broken = '{"tasks": {"a": true,}}'
    path = _write(tmp_path, broken)
    ok, message = set_task_enabled(path, "a", False)
    assert ok is False and "JSON" in message
    assert open(path, encoding="utf-8").read() == broken


def test_set_task_enabled_missing_file(tmp_path):
    ok, message = set_task_enabled(str(tmp_path / "nope.json"), "a", True)
    assert ok is False and "不存在" in message


def test_update_tasks_writes_many_in_one_go(tmp_path):
    """「全部启用 / 全部停止」：一次读一次写，别的字段不动。"""
    path = _write(tmp_path, {"version": 1, "tasks": {"a": True, "b": False}, "flow": "daily"})
    ok, message = update_tasks(path, {"a": False, "b": True, "c": True})
    assert ok is True and message == ""
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["tasks"] == {"a": False, "b": True, "c": True}
    assert doc["flow"] == "daily"
    assert set(load_plan(path).enabled_keys(_TaskSet({k: _Task() for k in "abc"}))) == {"b", "c"}


def test_update_tasks_with_empty_updates_is_a_noop(tmp_path):
    path = _write(tmp_path, {"tasks": {"a": True}})
    before = open(path, encoding="utf-8").read()
    assert update_tasks(path, {}) == (True, "")
    assert open(path, encoding="utf-8").read() == before


def test_save_plan_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "task_plan.json")
    assert save_plan({"version": 1, "tasks": {"a": True}}, path) is True
    assert os.path.isfile(path)
    assert not os.path.exists(path + ".tmp")


def test_real_taskset_can_be_enabled_end_to_end(tmp_path):
    """拿仓库里真实的 tasks/ 跑一遍：全部启用时的 key 顺序要和定义一致。"""
    taskset = load_tasks(os.path.join(ROOT, "tasks"), base_dir=ROOT)
    path = str(tmp_path / "task_plan.json")
    plan = ensure_plan(path, taskset, flow=taskset.flow_names()[0])
    # watch_only 的任务（红包这类）只由循环监控调用，不算进「本次跑哪些」
    expected = [key for key, task in taskset.tasks.items() if not task.watch_only]
    assert plan.enabled_keys(taskset) == expected


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


# ---------------------------------------------------------------------- 流程中文名
def _task_doc(flows, labels=None, task_key="rest"):
    doc = {
        "version": 1,
        "tasks": {task_key: {"desc": "休息", "steps": [{"action": "sleep", "seconds": 0.1}]}},
        "flows": flows,
    }
    if labels is not None:
        doc["flow_labels"] = labels
    return doc


def test_flow_labels_merge_and_fallback(tmp_path):
    from core.task_schema import load_tasks

    (tmp_path / "a.json").write_text(
        json.dumps(_task_doc({"daily": ["rest"]}, {"daily": "每日任务"}), ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(
        json.dumps(
            _task_doc({"quick": ["restQuick"]}, {"quick": "快速收菜"}, "restQuick"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    taskset = load_tasks(str(tmp_path), base_dir=str(tmp_path))
    assert taskset.flow_labels == {"daily": "每日任务", "quick": "快速收菜"}
    assert taskset.flow_label("daily") == "每日任务"
    # 没配中文名的流程回落到 key，界面不会出现空白
    assert taskset.flow_label("unknownFlow") == "unknownFlow"


def test_bad_flow_labels_warn_but_do_not_fail(tmp_path):
    from core.task_schema import load_tasks

    doc = _task_doc({"daily": ["rest"]}, {"daily": "", "quick": "快速收菜"})
    (tmp_path / "a.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    taskset = load_tasks(str(tmp_path), base_dir=str(tmp_path))
    assert taskset.flow_label("daily") == "daily"
    assert taskset.flow_label("quick") == "快速收菜"
    assert any("flow_labels" in warning for warning in taskset.warnings)
