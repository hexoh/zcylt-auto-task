"""core 层单元测试：坐标换算 / 模板匹配 / JSON 校验 / 执行器语义。

全部用合成图片和演练后端，不依赖真机。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# core 这一层离不开 numpy / opencv；没装就整体跳过，
# 纯逻辑测试（stepkit / 日志）放在 test_tools.py，不依赖图像库。
np = pytest.importorskip("numpy", reason="core 测试需要 numpy")
pytest.importorskip("cv2", reason="core 测试需要 opencv-python")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import vision  # noqa: E402
from core.backends import MockBackend  # noqa: E402
from core.coords import (  # noqa: E402
    Geometry,
    abs_to_rel1000,
    px_to_rel1000,
    rel1000_to_abs,
    rel1000_to_px,
    to_abs,
)
from core.errors import TaskSchemaError  # noqa: E402
from core.logger import Logger  # noqa: E402
from core.runner import Runner  # noqa: E402
from core.task_schema import load_tasks, validate_task  # noqa: E402
from core.vision import TemplateMatcher  # noqa: E402

FRAME_W, FRAME_H = 400, 300
TPL_W, TPL_H = 40, 30
TPL_AT = (120, 80)  # 模板左上角


# --------------------------------------------------------------------------- 工具
def make_frame(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frame = np.full((FRAME_H, FRAME_W, 3), 30, dtype=np.uint8)
    tpl = rng.integers(0, 255, (TPL_H, TPL_W, 3), dtype=np.uint8)
    x, y = TPL_AT
    frame[y : y + TPL_H, x : x + TPL_W] = tpl
    return frame


def save_png(path: str, img: np.ndarray) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ok, buf = vision.cv2.imencode(".png", img)
    assert ok
    buf.tofile(path)
    return path


def make_templates(tmp_path, seed: int = 1) -> dict:
    """把模板图落到 tmp_path/templates 下，返回相对路径。"""
    frame = make_frame(seed)
    x, y = TPL_AT
    tpl = frame[y : y + TPL_H, x : x + TPL_W]
    rel = "templates/ui/btn.png"
    save_png(os.path.join(str(tmp_path), *rel.split("/")), tpl)

    # 一张画面里没有的模板（else 分支用）
    other = np.random.default_rng(99).integers(0, 255, (20, 20, 3), dtype=np.uint8)
    rel_other = "templates/ui/other.png"
    save_png(os.path.join(str(tmp_path), *rel_other.split("/")), other)
    return {"hit": rel, "miss": rel_other, "frame": frame}


class FrameBackend(MockBackend):
    """固定返回一帧画面，记录所有动作。"""

    def __init__(self, frame: np.ndarray, config=None, logger=None):
        super().__init__(config or {}, logger)
        self._geom = Geometry(0, 0, frame.shape[1], frame.shape[0])
        self._frame = frame

    def screenshot(self):
        return self._frame.copy()


def write_tasks(tmp_path, doc: dict, name: str = "t.json") -> str:
    path = os.path.join(str(tmp_path), "tasks", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(doc, fp, ensure_ascii=False)
    return path


def build(tmp_path, doc, backend=None, variables=None, selection=None):
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    logger = Logger(log_dir=os.path.join(str(tmp_path), "logs"), echo=False)
    backend = backend or FrameBackend(make_frame(), logger=logger)
    runner = Runner(
        backend=backend,
        taskset=taskset,
        logger=logger,
        variables=variables or {},
        poll_interval=0.05,
        selection=selection,
    )
    return runner, backend, taskset, logger


QUICK_DEFAULTS = {"interval": 0.0, "timeout": 0.25, "threshold": 0.8, "after": 0.0}


# --------------------------------------------------------------------------- 坐标
def test_rel1000_roundtrip():
    assert rel1000_to_px(500, 500, 1000, 1000) == (500, 500)
    assert rel1000_to_px(0, 0, 1280, 720) == (0, 0)
    assert rel1000_to_px(1000, 1000, 1280, 720) == (1279, 719)
    assert px_to_rel1000(640, 360, 1280, 720) == (500, 500)
    assert rel1000_to_px(500, 500, 1280, 720) == (640, 360)


def test_geometry_offset_and_coord_kinds():
    geom = Geometry(100, 50, 1280, 720)
    assert to_abs(500, 500, "rel1000", geom) == (740, 410)
    assert to_abs(10, 20, "window", geom) == (110, 70)
    assert to_abs(10, 20, "abs", geom) == (10, 20)
    assert abs_to_rel1000(740, 410, geom) == (500, 500)
    assert rel1000_to_abs(0, 0, geom) == (100, 50)


def test_coord_aliases():
    geom = Geometry(0, 0, 100, 100)
    assert to_abs(10, 10, "px", geom) == (10, 10)
    assert to_abs(10, 10, None, geom) == (1, 1)  # 默认 rel1000


def test_geometry_validation():
    with pytest.raises(ValueError):
        Geometry(0, 0, 0, 100)


# --------------------------------------------------------------------------- 识图
def test_matcher_finds_template(tmp_path):
    tpl = make_templates(tmp_path)
    matcher = TemplateMatcher(threshold=0.85)
    match = matcher.find(tpl["frame"], os.path.join(str(tmp_path), tpl["hit"]))
    assert match is not None
    assert match.score > 0.99
    assert match.center == (TPL_AT[0] + TPL_W // 2, TPL_AT[1] + TPL_H // 2)


def test_matcher_threshold_rejects(tmp_path):
    tpl = make_templates(tmp_path)
    matcher = TemplateMatcher(threshold=0.999999)
    assert matcher.find(tpl["frame"], os.path.join(str(tmp_path), tpl["miss"])) is None


def test_matcher_region(tmp_path):
    tpl = make_templates(tmp_path)
    matcher = TemplateMatcher(threshold=0.8)
    path = os.path.join(str(tmp_path), tpl["hit"])
    assert matcher.find(tpl["frame"], path, region=[0, 0, 60, 60]) is None
    assert matcher.find(tpl["frame"], path, region=[100, 60, 100, 100]) is not None


def test_matcher_missing_template(tmp_path):
    from core.errors import TemplateMissing

    matcher = TemplateMatcher()
    with pytest.raises(TemplateMissing):
        matcher.find(make_frame(), os.path.join(str(tmp_path), "templates/none.png"))


def test_matcher_multiscale(tmp_path):
    tpl = make_templates(tmp_path)
    # 整帧缩到 80%，模板随之变成 0.8 倍，多尺度应该能兜住
    small = vision.cv2.resize(
        tpl["frame"], (int(FRAME_W * 0.8), int(FRAME_H * 0.8)), interpolation=vision.cv2.INTER_AREA
    )
    matcher = TemplateMatcher(threshold=0.85, multiscale=True)
    match = matcher.find(
        small, os.path.join(str(tmp_path), tpl["hit"]), scales=(0.6, 0.7, 0.8, 0.9, 1.0)
    )
    assert match is not None
    assert 0.75 <= match.scale <= 0.85


# --------------------------------------------------------------------------- schema
def test_load_project_tasks():
    taskset = load_tasks(os.path.join(ROOT, "tasks"), base_dir=ROOT)
    assert "daily" in taskset.flows
    assert "backHome" in taskset.tasks
    assert taskset.missing_templates  # 模板没拍，应该有缺失清单


def test_defaults_merge(tmp_path):
    doc = {
        "version": 1,
        "defaults": {"threshold": 0.7, "timeout": 3},
        "tasks": {"t": {"steps": [{"action": "image_tap", "template": "templates/ui/btn.png"}]}},
        "flows": {"f": ["t"]},
    }
    make_templates(tmp_path)
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    step = taskset.tasks["t"].steps[0]
    assert step["threshold"] == 0.7
    assert step["timeout"] == 3


def test_unknown_action_rejected(tmp_path):
    doc = {"tasks": {"t": {"steps": [{"action": "fly"}]}}, "flows": {"f": ["t"]}}
    path = write_tasks(tmp_path, doc)
    with pytest.raises(TaskSchemaError) as exc:
        load_tasks(path, base_dir=str(tmp_path))
    assert "未知 action" in str(exc.value)


def test_missing_required_field(tmp_path):
    doc = {"tasks": {"t": {"steps": [{"action": "tap", "x": 10}]}}}
    path = write_tasks(tmp_path, doc)
    with pytest.raises(TaskSchemaError) as exc:
        load_tasks(path, base_dir=str(tmp_path))
    assert "缺少字段 y" in str(exc.value)


def test_call_cycle_rejected(tmp_path):
    doc = {
        "tasks": {
            "a": {"steps": [{"action": "call", "task": "b"}]},
            "b": {"steps": [{"action": "call", "task": "a"}]},
        }
    }
    path = write_tasks(tmp_path, doc)
    with pytest.raises(TaskSchemaError) as exc:
        load_tasks(path, base_dir=str(tmp_path))
    assert "循环引用" in str(exc.value)


def test_flow_unknown_task_rejected(tmp_path):
    doc = {"tasks": {"a": {"steps": [{"action": "sleep", "seconds": 0}]}}, "flows": {"f": ["b"]}}
    path = write_tasks(tmp_path, doc)
    with pytest.raises(TaskSchemaError) as exc:
        load_tasks(path, base_dir=str(tmp_path))
    assert "不存在的任务" in str(exc.value)


def test_missing_template_is_warning(tmp_path):
    doc = {"tasks": {"a": {"steps": [{"action": "image_tap", "template": "templates/none.png"}]}}}
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    assert "templates/none.png" in taskset.missing_templates
    assert taskset.warnings


def test_unknown_step_field_warning(tmp_path):
    doc = {"tasks": {"a": {"steps": [{"action": "sleep", "seconds": 0, "tempalte": "x"}]}}}
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    assert any("tempalte" in w for w in taskset.warnings)


# ------------------------------------------------------------------- step_count
def test_step_count_match_no_warning(tmp_path):
    doc = {
        "tasks": {
            "t": {
                "step_count": 2,
                "steps": [{"action": "sleep", "seconds": 0}, {"action": "sleep", "seconds": 0}],
            }
        }
    }
    taskset = load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path))
    task = taskset.tasks["t"]
    assert task.step_count == 2
    assert task.declared_steps == 2
    assert not task.steps_mismatch
    assert not [w for w in taskset.warnings if "总步数" in w]


def test_step_count_absent_means_unlimited(tmp_path):
    doc = {"tasks": {"t": {"steps": [{"action": "sleep", "seconds": 0}]}}}
    task = load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path)).tasks["t"]
    assert task.step_count == 0
    assert task.declared_steps == 1  # 未声明时回落到实际步数
    assert not task.steps_mismatch


def test_step_count_mismatch_warns_on_load(tmp_path):
    doc = {"tasks": {"t": {"step_count": 5, "steps": [{"action": "sleep", "seconds": 0}]}}}
    taskset = load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path))
    assert taskset.tasks["t"].steps_mismatch
    assert any("总步数" in w and "5" in w for w in taskset.warnings)


def test_step_count_counts_only_top_level(tmp_path):
    """声明的是顶层步骤数：loop / exists 内部的步骤不计数。"""
    doc = {
        "tasks": {
            "t": {
                "step_count": 2,
                "steps": [
                    {
                        "action": "loop",
                        "count": 3,
                        "do": [
                            {"action": "sleep", "seconds": 0},
                            {"action": "sleep", "seconds": 0},
                        ],
                    },
                    {"action": "sleep", "seconds": 0},
                ],
            }
        }
    }
    task = load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path)).tasks["t"]
    assert not task.steps_mismatch


def test_step_count_invalid_rejected(tmp_path):
    for idx, bad in enumerate((0, -2, "3", 1.5)):
        doc = {"tasks": {"t": {"step_count": bad, "steps": [{"action": "sleep", "seconds": 0}]}}}
        path = write_tasks(tmp_path, doc, name=f"bad{idx}.json")
        with pytest.raises(TaskSchemaError) as exc:
            load_tasks(path, base_dir=str(tmp_path))
        assert "step_count" in str(exc.value)


def test_validate_task_blocks_step_count_mismatch(tmp_path):
    body = {"step_count": 3, "steps": [{"action": "sleep", "seconds": 0}]}
    check = validate_task("t", body, str(tmp_path))
    assert not check.ok
    assert any("总步数" in e for e in check.errors)

    body = {"step_count": 1, "steps": [{"action": "sleep", "seconds": 0}]}
    check = validate_task("t", body, str(tmp_path))
    assert check.ok
    assert check.declared_steps == 1
    assert "声明的总步数一致" in check.summary()


# --------------------------------------------------------------------------- runner
def test_image_tap_hits_center(tmp_path):
    tpl = make_templates(tmp_path)
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {"t": {"steps": [{"action": "image_tap", "template": tpl["hit"]}]}},
        "flows": {"f": ["t"]},
    }
    runner, backend, _, _ = build(tmp_path, doc)
    report = runner.run(["t"])
    assert report.ok
    assert backend.actions[0][0] == "tap"
    assert backend.actions[0][1:] == (TPL_AT[0] + TPL_W // 2, TPL_AT[1] + TPL_H // 2)


def test_tap_rel1000_conversion(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {"t": {"steps": [{"action": "tap", "x": 500, "y": 500}]}},
        "flows": {"f": ["t"]},
    }
    runner, backend, _, _ = build(tmp_path, doc)
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", FRAME_W // 2, FRAME_H // 2)]


def test_swipe_and_times(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "t": {
                "steps": [
                    {"action": "swipe", "x1": 0, "y1": 0, "x2": 1000, "y2": 1000, "duration": 200},
                    {"action": "tap", "x": 100, "y": 100, "times": 3, "interval": 0},
                ]
            }
        },
    }
    runner, backend, _, _ = build(tmp_path, doc)
    assert runner.run(["t"]).ok
    kinds = [a[0] for a in backend.actions]
    assert kinds == ["swipe", "tap", "tap", "tap"]


def test_exists_then_branch(tmp_path):
    tpl = make_templates(tmp_path)
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "t": {
                "steps": [
                    {
                        "action": "exists",
                        "template": tpl["hit"],
                        "then": [{"action": "tap", "x": 10, "y": 10}],
                        "else": [{"action": "tap", "x": 90, "y": 90}],
                    }
                ]
            }
        },
    }
    runner, backend, _, _ = build(tmp_path, doc)
    assert runner.run(["t"]).ok
    # rel1000 (10, 10) 在 400x300 画面上 → (4, 3)
    assert backend.actions == [("tap", 4, 3)]


def test_exists_else_branch(tmp_path):
    tpl = make_templates(tmp_path)
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "t": {
                "steps": [
                    {
                        "action": "exists",
                        "template": tpl["miss"],
                        "then": [{"action": "tap", "x": 10, "y": 10}],
                        "else": [{"action": "tap", "x": 900, "y": 900}],
                    }
                ]
            }
        },
    }
    runner, backend, _, _ = build(tmp_path, doc)
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 360, 270)]


def test_wait_image_timeout_and_next_task(tmp_path):
    tpl = make_templates(tmp_path)
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "bad": {
                "steps": [
                    {"action": "wait_image", "template": tpl["miss"], "on_fail": "next_task"}
                ]
            },
            "good": {"steps": [{"action": "tap", "x": 500, "y": 500}]},
        },
        "flows": {"f": ["bad", "good"]},
    }
    runner, backend, _, _ = build(tmp_path, doc)
    report = runner.run(["bad", "good"])
    assert report.tasks_skipped == 1
    assert report.tasks_ok == 1
    assert backend.actions == [("tap", FRAME_W // 2, FRAME_H // 2)]


def test_abort_policy(tmp_path):
    tpl = make_templates(tmp_path)
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "bad": {
                "steps": [
                    {"action": "wait_image", "template": tpl["miss"], "on_fail": "abort"}
                ]
            },
            "good": {"steps": [{"action": "tap", "x": 500, "y": 500}]},
        },
    }
    runner, backend, _, _ = build(tmp_path, doc)
    report = runner.run(["bad", "good"])
    assert not report.ok
    assert report.aborted_reason
    assert backend.actions == []


def test_call_and_loop(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "atom": {"steps": [{"action": "tap", "x": 100, "y": 100}]},
            "main": {
                "steps": [
                    {"action": "call", "task": "atom"},
                    {"action": "loop", "count": 2, "do": [{"action": "call", "task": "atom"}]},
                ]
            },
        },
    }
    runner, backend, _, _ = build(tmp_path, doc)
    assert runner.run(["main"]).ok
    assert len([a for a in backend.actions if a[0] == "tap"]) == 3


def test_call_cycle_at_runtime_blocked(tmp_path):
    doc = {
        "tasks": {
            "a": {"steps": [{"action": "call", "task": "b"}]},
            "b": {"steps": [{"action": "sleep", "seconds": 0}]},
        }
    }
    runner, _, _, _ = build(tmp_path, doc)
    # 直接调用自身，模拟嵌套过深/循环
    assert runner.run_task("a", depth=0)
    runner._stack = ["a"]
    assert runner.run_task("a") is False


def test_text_variable_expansion(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {"t": {"steps": [{"action": "text", "value": "code-${redeem_code}"}]}},
    }
    runner, backend, _, _ = build(tmp_path, doc, variables={"redeem_code": "ABC123"})
    assert runner.run(["t"]).ok
    assert backend.actions == [("text", "code-ABC123")]


def test_missing_template_file_fails_task(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {"t": {"steps": [{"action": "image_tap", "template": "templates/none.png"}]}},
    }
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    logger = Logger(log_dir=os.path.join(str(tmp_path), "logs"), echo=False)
    runner = Runner(FrameBackend(make_frame(), logger=logger), taskset, logger)
    report = runner.run(["t"])
    assert not report.ok
    assert report.tasks_failed == 1


def test_stop_event_interrupts(tmp_path):
    doc = {
        "defaults": QUICK_DEFAULTS,
        "tasks": {"t": {"steps": [{"action": "sleep", "seconds": 5}]}},
    }
    runner, _, _, _ = build(tmp_path, doc)
    runner.stop_event.set()
    report = runner.run(["t"])
    assert not report.ok
    assert "停止" in report.aborted_reason


def test_disabled_task_skipped(tmp_path):
    doc = {
        "tasks": {
            "off": {"enabled": False, "steps": [{"action": "tap", "x": 1, "y": 1}]},
        }
    }
    runner, backend, _, _ = build(tmp_path, doc)
    report = runner.run(["off"])
    assert report.tasks_skipped == 1
    assert backend.actions == []


def test_flow_resolution_filters(tmp_path):
    doc = {
        "tasks": {"a": {"steps": []}, "b": {"steps": []}},
        "flows": {"f": ["a", "b"]},
    }
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    assert taskset.resolve_flow("f") == ["a", "b"]
    assert taskset.resolve_flow("f", enabled=["b"]) == ["b"]
    with pytest.raises(KeyError):
        taskset.resolve_flow("nope")


# --------------------------------------------------------------------------- mock
def test_mock_backend_canvas_matches_templates(tmp_path):
    tpl = make_templates(tmp_path)
    templates_dir = os.path.join(str(tmp_path), "templates")
    backend = MockBackend({"size": (800, 600)}, templates_dir=templates_dir)
    backend.connect()
    frame = backend.screenshot()
    assert frame.shape[:2] == (600, 800)
    matcher = TemplateMatcher(threshold=0.9)
    for rel in (tpl["hit"], tpl["miss"]):
        assert matcher.find(frame, os.path.join(str(tmp_path), rel)) is not None


def test_logger_writes_file(tmp_path):
    logger = Logger(log_dir=str(tmp_path / "logs"), echo=False)
    logger.info("hello")
    logger.warn("careful")
    logger.section("done")
    with open(logger.path, encoding="utf-8") as fp:
        content = fp.read()
    assert "hello" in content
    assert "WARN" in content
    assert len(logger.records) == 3


# --------------------------------------------------------------------------- tap_list
def tap_list_doc(items, **extra) -> dict:
    step = {"action": "tap_list", "id": "rows", "coord": "abs", "interval": 0, "after": 0, "items": items}
    step.update(extra)
    return {"version": 1, "defaults": QUICK_DEFAULTS, "tasks": {"t": {"desc": "t", "steps": [step]}}}


ROWS = [
    {"id": "r1", "name": "第 1 个奖励", "x": 100, "y": 100},
    {"id": "r2", "name": "第 2 个奖励", "x": 150, "y": 200},
    {"id": "r3", "name": "第 3 个奖励", "x": 200, "y": 250},
]


def test_tap_list_selection_hits_only_chosen(tmp_path):
    runner, backend, _, _ = build(tmp_path, tap_list_doc(ROWS), selection={"rows": ["r1", "r3"]})
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 100, 100), ("tap", 200, 250)]


def test_tap_list_default_selected_false_skips_all(tmp_path):
    runner, backend, _, _ = build(tmp_path, tap_list_doc(ROWS, default_selected=False))
    assert runner.run(["t"]).ok
    assert backend.actions == []


def test_tap_list_respects_max(tmp_path):
    runner, backend, _, _ = build(tmp_path, tap_list_doc(ROWS, max=2))
    assert runner.run(["t"]).ok
    assert len(backend.actions) == 2


def test_tap_list_times_repeats_each_point(tmp_path):
    runner, backend, _, _ = build(
        tmp_path, tap_list_doc([ROWS[0]], times=2), selection={"rows": ["r1"]}
    )
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 100, 100), ("tap", 100, 100)]


def test_tap_list_requires_items(tmp_path):
    doc = tap_list_doc(ROWS)
    doc["tasks"]["t"]["steps"][0]["items"] = []
    with pytest.raises(TaskSchemaError):
        load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path))


def test_tap_list_rejects_duplicate_ids(tmp_path):
    bad = [ROWS[0], dict(ROWS[1], id="r1")]
    with pytest.raises(TaskSchemaError):
        load_tasks(write_tasks(tmp_path, tap_list_doc(bad)), base_dir=str(tmp_path))


# --------------------------------------------------------------------------- image_tap 多目标
def make_frame_multi(ys=(60, 170)) -> np.ndarray:
    frame = np.full((FRAME_H, FRAME_W, 3), 30, dtype=np.uint8)
    rng = np.random.default_rng(1)
    tpl = rng.integers(0, 255, (TPL_H, TPL_W, 3), dtype=np.uint8)
    for y in ys:
        frame[y : y + TPL_H, TPL_AT[0] : TPL_AT[0] + TPL_W] = tpl
    return frame


def image_tap_multi_build(tmp_path, **step_extra):
    tpl = make_templates(tmp_path)
    frame = make_frame_multi()
    backend = FrameBackend(frame)
    step = {"action": "image_tap", "template": tpl["hit"], "threshold": 0.9, "after": 0, "interval": 0}
    step.update(step_extra)
    doc = {"version": 1, "defaults": QUICK_DEFAULTS, "tasks": {"t": {"steps": [step]}}}
    runner, backend, _, _ = build(tmp_path, doc, backend=backend)
    return runner, backend


def test_image_tap_all_clicks_every_hit(tmp_path):
    runner, backend = image_tap_multi_build(tmp_path, all=True, limit=10)
    assert runner.run(["t"]).ok
    assert len(backend.actions) == 2
    ys = [action[2] for action in backend.actions]
    assert ys == sorted(ys), "应按从上到下点击"


def test_image_tap_index_clicks_nth(tmp_path):
    runner, backend = image_tap_multi_build(tmp_path, index=2)
    assert runner.run(["t"]).ok
    assert len(backend.actions) == 1
    assert backend.actions[0][2] > backend.actions[0][1] // 2  # 第二个更靠下


def test_image_tap_limit_caps_clicks(tmp_path):
    runner, backend = image_tap_multi_build(tmp_path, all=True, limit=1)
    assert runner.run(["t"]).ok
    assert len(backend.actions) == 1


# --------------------------------------------------------------------------- 单任务校验
def test_validate_task_ok(tmp_path):
    from core.task_schema import validate_task

    body = {"desc": "领奖", "steps": [{"action": "tap", "x": 100, "y": 200}]}
    result = validate_task("claim", body, str(tmp_path))
    assert result.ok, result.errors
    assert result.steps[0]["coord"] == "rel1000"


def test_validate_task_reports_missing_field(tmp_path):
    from core.task_schema import validate_task

    result = validate_task("claim", {"steps": [{"action": "tap", "x": 1}]}, str(tmp_path))
    assert not result.ok
    assert any("缺少字段 y" in e for e in result.errors)


def test_validate_task_flags_missing_template(tmp_path):
    from core.task_schema import validate_task

    body = {"steps": [{"action": "wait_image", "template": "templates/ui/none.png"}]}
    result = validate_task("t", body, str(tmp_path))
    assert result.ok
    assert result.missing_templates == ["templates/ui/none.png"]


def test_upsert_task_document_keeps_others():
    from core.task_schema import upsert_task_document

    doc = {"version": 1, "tasks": {"a": {"steps": []}}, "flows": {"daily": ["a"]}}
    merged = upsert_task_document(doc, "b", {"steps": []}, "daily")
    assert set(merged["tasks"]) == {"a", "b"}
    assert merged["flows"]["daily"] == ["a", "b"]

    again = upsert_task_document(merged, "b", {"steps": []}, "daily")
    assert again["flows"]["daily"] == ["a", "b"], "重复写入不应重复追加"



def test_runner_ui_log_is_task_level(tmp_path):
    ui_lines = []
    doc = {
        "version": 1,
        "defaults": QUICK_DEFAULTS,
        "tasks": {"collectTax": {"desc": "收取税收", "steps": [{"action": "tap", "x": 1, "y": 2}]}},
    }
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    logger = Logger(
        log_dir=os.path.join(str(tmp_path), "logs"), echo=False, on_ui=ui_lines.append
    )
    runner = Runner(
        backend=FrameBackend(make_frame(), logger=logger),
        taskset=taskset,
        logger=logger,
        poll_interval=0.05,
    )
    assert runner.run(["collectTax"]).ok
    assert any(line.endswith("收取税收 成功") for line in ui_lines), ui_lines
    assert not any("click" in line or "tap" in line or "(" in line for line in ui_lines)
    assert not any("[INFO]" in line or "[WARN]" in line for line in ui_lines)


# --------------------------------------------------------------------------- 点位勾选（R4）
def test_point_groups_expose_task_scoped_key(tmp_path):
    doc = tap_list_doc(ROWS)
    doc["tasks"]["t"]["desc"] = "领取奖励"
    path = write_tasks(tmp_path, doc)
    taskset = load_tasks(path, base_dir=str(tmp_path))
    groups = taskset.point_groups()
    assert len(groups) == 1
    group = groups[0]
    assert group["key"] == "t::rows"
    assert group["task_title"] == "领取奖励"
    assert [item["id"] for item in group["items"]] == ["r1", "r2", "r3"]
    assert group["default_selected"] is True


def test_point_groups_walk_loop_and_skip_disabled(tmp_path):
    step = {
        "action": "loop",
        "count": 1,
        "do": [
            {"action": "tap_list", "id": "inner", "items": ROWS},
        ],
    }
    doc = {
        "version": 1,
        "defaults": QUICK_DEFAULTS,
        "tasks": {
            "nested": {"desc": "循环领奖", "steps": [step]},
            "off": {"desc": "停用", "enabled": False, "steps": [{"action": "tap_list", "id": "z", "items": ROWS}]},
        },
    }
    taskset = load_tasks(write_tasks(tmp_path, doc), base_dir=str(tmp_path))
    keys = [g["key"] for g in taskset.point_groups()]
    assert keys == ["nested::inner"], "循环里的 tap_list 也要能勾选，停用任务不显示"


def test_tap_list_uses_task_scoped_selection(tmp_path):
    runner, backend, _, _ = build(tmp_path, tap_list_doc(ROWS), selection={"t::rows": ["r2"]})
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 150, 200)]


def test_tap_list_bare_step_id_still_works(tmp_path):
    """老配置（只有 step id）不能因为新键格式而失效。"""
    runner, backend, _, _ = build(tmp_path, tap_list_doc(ROWS), selection={"rows": ["r1"]})
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 100, 100)]


def test_tap_list_empty_selection_skips_whole_step(tmp_path):
    ui_lines = []
    path = write_tasks(tmp_path, tap_list_doc(ROWS))
    taskset = load_tasks(path, base_dir=str(tmp_path))
    logger = Logger(
        log_dir=os.path.join(str(tmp_path), "logs"), echo=False, on_ui=ui_lines.append
    )
    backend = FrameBackend(make_frame(), logger=logger)
    runner = Runner(
        backend=backend,
        taskset=taskset,
        logger=logger,
        poll_interval=0.05,
        selection={"t::rows": []},
    )
    assert runner.run(["t"]).ok
    assert backend.actions == []
    # 跳过明细只落盘，界面仍只有任务级一行
    text = open(logger.path, encoding="utf-8").read()
    assert "未勾选任何点位" in text
    assert not any("点位" in line for line in ui_lines), ui_lines


def test_tap_list_ui_log_stays_task_level(tmp_path):
    """勾了 2 个点位，界面也只出「任务 成功」一行，不刷点位明细。"""
    ui_lines = []
    path = write_tasks(tmp_path, tap_list_doc(ROWS))
    taskset = load_tasks(path, base_dir=str(tmp_path))
    logger = Logger(
        log_dir=os.path.join(str(tmp_path), "logs"), echo=False, on_ui=ui_lines.append
    )
    backend = FrameBackend(make_frame(), logger=logger)
    runner = Runner(
        backend=backend,
        taskset=taskset,
        logger=logger,
        poll_interval=0.05,
        selection={"t::rows": ["r1", "r3"]},
    )
    assert runner.run(["t"]).ok
    assert backend.actions == [("tap", 100, 100), ("tap", 200, 250)]
    assert not any("第 1 个奖励" in line or "第 3 个奖励" in line for line in ui_lines), ui_lines
    assert any(line.endswith("t 成功") for line in ui_lines), ui_lines
    # 明细在文件里
    text = open(logger.path, encoding="utf-8").read()
    assert "第 1 个奖励" in text and "跳过未勾选点位 r2" in text
