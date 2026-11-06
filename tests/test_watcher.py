"""循环监控（core/watcher.py）与设备互斥（core/device.py）的单元测试。

全部用合成画面 + 假后端，不需要真机、也不真的等 5 分钟。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

np = pytest.importorskip("numpy", reason="监控测试需要 numpy")
pytest.importorskip("cv2", reason="监控测试需要 opencv-python")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.device import DeviceGate  # noqa: E402
from core.vision import TemplateMatcher  # noqa: E402
from core.watcher import (  # noqa: E402
    MIN_INTERVAL,
    RuleRuntime,
    WatchRule,
    WatcherService,
    parse_rules,
)

W, H = 640, 360


# ---------------------------------------------------------------------- 造图
def make_frame(with_packet: bool = True, shift: int = 0):
    """合成一帧：灰底 + 几个 UI 块 + 一个「有花纹」的红包。

    红包必须带内部花纹：只有红底 + 金边这种"一条水平边"的图案，在
    TM_CCOEFF_NORMED 下和其他任意边缘都能算出接近 1.0 的分数（归一化相关
    对亮度/对比度不敏感），会把这个测试变成假阳性。真实游戏画面同理。
    """
    import cv2

    frame = np.full((H, W, 3), 46, np.uint8)
    frame[40:120, 40:200] = (90, 90, 110)
    frame[250:330, 480:620] = (70, 120, 90)
    if with_packet:
        x, y = 260 + shift, 120
        cv2.rectangle(frame, (x, y), (x + 60, y + 80), (30, 30, 190), -1)
        cv2.rectangle(frame, (x, y), (x + 60, y + 80), (40, 200, 240), 2)
        frame[y + 10 : y + 22, x + 6 : x + 54] = (40, 200, 240)
        cv2.circle(frame, (x + 30, y + 46), 11, (250, 250, 250), -1)
        cv2.rectangle(frame, (x + 12, y + 64), (x + 48, y + 72), (20, 20, 120), -1)
    return frame


@pytest.fixture()
def template_path(tmp_path):
    import cv2

    path = tmp_path / "red_packet.png"
    cv2.imwrite(str(path), make_frame()[120:200, 260:320].copy())
    return path


class FakeBackend:
    """按顺序吐帧的假后端；帧用完后一直重复最后一帧。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.shots = 0
        self.closed = False

    def screenshot(self):
        index = min(self.shots, len(self.frames) - 1)
        self.shots += 1
        return self.frames[index].copy()

    def close(self):
        self.closed = True


class FakeTaskset:
    def __init__(self, keys=("claimRedPacket",)):
        from core.task_schema import TaskDef

        self.tasks = {key: TaskDef(key=key, desc=key) for key in keys}
        self.defaults = {"threshold": 0.85}


def make_service(tmp_path, template_path, **rule_kwargs):
    rule = WatchRule(
        key="redPacket",
        name="红包",
        task="claimRedPacket",
        template=str(template_path),
        **rule_kwargs,
    )
    service = WatcherService(str(tmp_path), gate=DeviceGate())
    service.configure([rule], mode="adb", backend={}, taskset=FakeTaskset())
    service._matcher = TemplateMatcher(threshold=0.85)
    return service


# ---------------------------------------------------------------------- 规则解析
def test_parse_rules_defaults_and_order():
    rules, problems = parse_rules(
        [
            {"key": "a", "task": "claimRedPacket", "template": "t.png"},
            {"key": "b", "name": "签到", "task": "claimSign", "template": "s.png", "interval_seconds": 60},
        ]
    )
    assert problems == []
    assert [rule.key for rule in rules] == ["a", "b"]
    assert rules[0].interval_seconds == 300
    assert rules[0].name == "a"  # 没写 name 时用 key
    assert rules[1].interval_label() == "1 分钟"


def test_parse_rules_rejects_broken_entries():
    rules, problems = parse_rules(
        [
            {"key": "noTask", "template": "t.png"},
            {"key": "a", "task": "x", "template": "t.png"},
            {"key": "a", "task": "y", "template": "t.png"},
            "not-an-object",
        ]
    )
    assert [rule.key for rule in rules] == ["a"]
    assert len(problems) == 3


def test_parse_rules_clamps_tiny_interval():
    rules, problems = parse_rules(
        [{"key": "a", "task": "t", "template": "t.png", "interval_seconds": 0.5}]
    )
    assert rules[0].interval_seconds == MIN_INTERVAL
    assert any("太小" in problem for problem in problems)


def test_parse_rules_region_must_be_four_numbers():
    rules, problems = parse_rules(
        [
            {"key": "a", "task": "t", "template": "t.png", "region": [1, 2, 3]},
            {"key": "b", "task": "t", "template": "t.png", "region": [10, 20, 100, 80]},
        ]
    )
    assert rules[0].region is None
    assert rules[1].region == [10, 20, 100, 80]
    assert any("region" in problem for problem in problems)


# ---------------------------------------------------------------------- 冷却 / 配额
def test_cooldown_blocks_repeat_claim():
    runtime = RuleRuntime(rule=WatchRule(key="a", cooldown_seconds=600))
    now = 1000.0
    runtime.record_hit(now)
    allowed, why = runtime.can_claim(now + 10)
    assert allowed is False and "冷却" in why
    allowed, _ = runtime.can_claim(now + 601)
    assert allowed is True


def test_hourly_quota_blocks_and_expires():
    runtime = RuleRuntime(rule=WatchRule(key="a", cooldown_seconds=0, max_per_hour=2))
    now = 5000.0
    runtime.record_hit(now - 100)
    runtime.record_hit(now - 50)
    allowed, why = runtime.can_claim(now)
    assert allowed is False and "上限" in why
    # 一小时后配额恢复
    allowed, _ = runtime.can_claim(now + 3700)
    assert allowed is True


# ---------------------------------------------------------------------- 互斥锁
def test_device_gate_is_exclusive_and_times_out():
    gate = DeviceGate()
    order = []
    gate.acquire("主流程")
    started = threading.Event()

    def worker():
        started.set()
        with gate.hold("监控", timeout=0.2) as locked:
            order.append(locked)

    thread = threading.Thread(target=worker)
    thread.start()
    started.wait(1)
    thread.join(1.5)
    assert order == [False]  # 拿不到，返回 False 而不是死等

    gate.release()
    assert gate.busy is False
    with gate.hold("监控", timeout=1) as locked:
        assert locked is True


# ---------------------------------------------------------------------- 巡检
def test_hit_triggers_claim(tmp_path, template_path):
    service = make_service(tmp_path, template_path, confirm_frames=1, threshold=0.9)
    service._backend = FakeBackend([make_frame()])
    claimed = []
    service._claim = lambda runtime: claimed.append(runtime.rule.key)

    runtime = service._runtimes[0]
    service._inspect(runtime, service._grab())
    assert claimed == ["redPacket"]
    assert runtime.last_score > 0.9


def test_no_packet_no_claim(tmp_path, template_path):
    service = make_service(tmp_path, template_path, confirm_frames=1)
    service._backend = FakeBackend([make_frame(with_packet=False)])
    claimed = []
    service._claim = lambda runtime: claimed.append(runtime.rule.key)

    runtime = service._runtimes[0]
    service._inspect(runtime, service._grab())
    assert claimed == []
    assert "未发现" in runtime.note


def test_single_frame_flash_is_rejected(tmp_path, template_path):
    """第一帧有、第二帧没了 → 判为误报，不点击。"""
    service = make_service(tmp_path, template_path, confirm_frames=2)
    service._backend = FakeBackend([make_frame(), make_frame(with_packet=False)])
    claimed = []
    service._claim = lambda runtime: claimed.append(runtime.rule.key)

    runtime = service._runtimes[0]
    service._inspect(runtime, service._grab())
    assert claimed == []
    assert "误报" in runtime.note


def test_cooldown_prevents_second_claim(tmp_path, template_path):
    service = make_service(tmp_path, template_path, confirm_frames=1, cooldown_seconds=600)
    service._backend = FakeBackend([make_frame()])
    claimed = []
    service._claim = lambda runtime: claimed.append(runtime.rule.key)

    runtime = service._runtimes[0]
    runtime.record_hit(time.time())  # 刚领过
    service._inspect(runtime, service._grab())
    assert claimed == []
    assert "冷却" in runtime.note


# ---------------------------------------------------------------------- 启动校验
def test_start_refuses_when_template_missing(tmp_path):
    service = WatcherService(str(tmp_path), gate=DeviceGate())
    rules, problems = parse_rules(
        [{"key": "a", "task": "claimRedPacket", "template": "templates/event/nope.png"}]
    )
    service.configure(rules, mode="adb", backend={}, taskset=FakeTaskset(), problems=problems)
    ok, message = service.start()
    assert ok is False
    assert "缺少模板图" in message
    assert service.state()["message"] == message


def test_start_refuses_in_mock_mode(tmp_path, template_path):
    service = make_service(tmp_path, template_path)
    service._mode = "mock"
    ok, message = service.start()
    assert ok is False and "演练" in message


def test_start_refuses_unknown_task(tmp_path, template_path):
    service = make_service(tmp_path, template_path)
    service._taskset = FakeTaskset(keys=("otherTask",))
    ok, message = service.start()
    assert ok is False and "任务不存在" in message


def test_state_snapshot_has_ui_fields(tmp_path, template_path):
    service = make_service(tmp_path, template_path)
    state = service.state()
    assert state["running"] is False
    rule = state["rules"][0]
    for field in ("key", "name", "interval", "note", "hits"):
        assert field in rule
    assert rule["interval"] == "5 分钟"


def test_inspect_runs_real_task_through_runner(tmp_path, template_path):
    """命中后真的通过 Runner 跑一遍任务（用 sleep 任务，不依赖模板图）。"""
    from core.logger import Logger
    from core.task_schema import load_tasks

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "evt.json").write_text(
        """{
          "version": 1,
          "tasks": {
            "claimRedPacket": {
              "desc": "领取红包",
              "watch_only": true,
              "steps": [{"id": "wait", "action": "sleep", "seconds": 0.01}]
            }
          }
        }""",
        encoding="utf-8",
    )
    taskset = load_tasks(str(tasks_dir), base_dir=str(tmp_path))

    service = WatcherService(str(tmp_path), gate=DeviceGate())
    rule = WatchRule(
        key="redPacket", name="红包", task="claimRedPacket",
        template=str(template_path), confirm_frames=1, threshold=0.9,
    )
    service.configure([rule], mode="adb", backend={}, taskset=taskset)
    service._matcher = TemplateMatcher(threshold=0.9)
    service._backend = FakeBackend([make_frame()])
    service._logger = Logger(log_dir=str(tmp_path / "logs"), level="info", echo=False)
    service._running = True

    runtime = service._runtimes[0]
    service._inspect(runtime, service._grab())

    assert runtime.note == "已领取"
    assert runtime.hits == 1
    assert runtime.last_hit is not None
