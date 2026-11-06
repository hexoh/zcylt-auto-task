"""tools 层单元测试：步骤合成内核 + 日志双通道。

这些用例不依赖 numpy / opencv / tkinter，装了 Python 就能跑：

    .venv/bin/python -m pytest tests/test_tools.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.logger import Logger  # noqa: E402
from tools import stepkit  # noqa: E402


# --------------------------------------------------------------------------- 日志口径
def test_logger_ui_channel_has_no_level(tmp_path):
    """界面日志只收中文短句；详细行带 INFO 只进文件和 on_log。"""
    ui_lines, detail_lines = [], []
    logger = Logger(
        log_dir=str(tmp_path),
        echo=False,
        on_log=detail_lines.append,
        on_ui=ui_lines.append,
    )
    logger.info("✔ 任务完成 collectTax（1.2s）")
    assert detail_lines and "[INFO]" in detail_lines[0]
    assert ui_lines == [], "详细日志不应推给界面"

    logger.ui("收取税收 成功")
    assert ui_lines and ui_lines[0].endswith("收取税收 成功")
    assert "[INFO]" not in ui_lines[0] and "collectTax" not in ui_lines[0]

    text = open(logger.path, encoding="utf-8").read()
    assert "收取税收 成功" in text, "界面短句也要落盘，便于对照"




# --------------------------------------------------------------------------- stepkit
def test_auto_key_handles_english_and_chinese():
    assert stepkit.auto_key("claim daily rewards") == "claimDailyRewards"
    assert stepkit.auto_key("领取每日奖励") == "task1"
    assert stepkit.auto_key("领取每日奖励", ["task1"]) == "task2"
    assert stepkit.auto_key("claim daily rewards", ["claimDailyRewards"]) == "claimDailyRewards2"


def test_template_rel_path_groups_by_action():
    assert (
        stepkit.template_rel_path("claimRewards", 3, "image_tap")
        == "templates/ui/claimRewards_03.png"
    )
    assert (
        stepkit.template_rel_path("claimRewards", 1, "wait_image", "确认按钮")
        == "templates/ui/claimRewards_01_确认按钮.png"
    )
    assert stepkit.template_rel_path("a/b", 1, "exists") == "templates/ui/a_b_01.png"


def test_make_tap_and_swipe_use_rel1000():
    step = stepkit.make_tap(512, 845)
    assert step["coord"] == "rel1000"
    assert (step["x"], step["y"]) == (512, 845)
    assert "times" not in step, "单次点击不写多余字段"
    assert stepkit.make_tap(1, 2, times=3)["times"] == 3

    swipe = stepkit.make_swipe(500, 800, 500, 300)
    assert (swipe["x1"], swipe["y1"], swipe["x2"], swipe["y2"]) == (500, 800, 500, 300)
    assert swipe["duration"] == 400


def test_make_tap_list_gives_each_point_an_id():
    step = stepkit.make_tap_list([(10, 20), (30, 40)], step_id="reward_rows")
    assert [item["id"] for item in step["items"]] == ["p1", "p2"]
    assert [item["name"] for item in step["items"]] == ["第 1 个", "第 2 个"]
    assert step["default_selected"] is True
    assert step["id"] == "reward_rows"


def test_describe_step_for_list():
    label, detail = stepkit.describe_step(stepkit.make_tap(512, 588))
    assert "点击" in label and "512" in detail

    label, detail = stepkit.describe_step(stepkit.make_tap_list([(1, 2), (3, 4)]))
    assert "点位列表" in label and "2 个" in detail


def test_generated_steps_pass_schema(tmp_path):
    from core.task_schema import validate_task
    tpl = os.path.join(str(tmp_path), "templates", "ui", "btn.png")
    os.makedirs(os.path.dirname(tpl), exist_ok=True)
    with open(tpl, "wb") as fp:  # 内容无所谓，校验只看文件在不在
        fp.write(b"\x89PNG\r\n\x1a\n")

    steps = [
        stepkit.make_tap(100, 200),
        stepkit.make_swipe(500, 800, 500, 300),
        stepkit.make_tap_list([(10, 20), (30, 40)]),
        stepkit.make_image_step("image_tap", "templates/ui/btn.png"),
        stepkit.make_sleep(1),
        stepkit.make_text("hello"),
        stepkit.make_key("back"),
    ]
    result = validate_task("t", {"desc": "t", "steps": steps}, str(tmp_path))
    assert result.ok, result.errors
    assert result.missing_templates == []


def test_unique_step_id_avoids_duplicate_point_lists():
    """一个任务里录两段点位列表，步骤 id 不能撞（界面靠它分组）。"""
    from tools import stepkit

    steps = [stepkit.make_tap_list([(1, 2)], step_id="claim_rows")]
    assert stepkit.unique_step_id("claim_rows", steps) == "claim_rows_2"
    steps.append(stepkit.make_tap_list([(3, 4)], step_id="claim_rows_2"))
    assert stepkit.unique_step_id("claim_rows", steps) == "claim_rows_3"
    assert stepkit.unique_step_id("other", steps) == "other"
    assert stepkit.unique_step_id("", []) == "rows"
