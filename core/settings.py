"""本地配置读写（config.json）。

UI 上次填的设备地址、窗口标题、兑换码等都存这里，下次打开自动回填。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Optional

from .features import FEATURE_DEFAULTS

DEFAULT_CONFIG = {
    "mode": "adb",
    "tasks_path": "tasks",
    # 任务计划：控制哪些任务启用、点位怎么选（见 core/task_plan.py）
    "plan_path": "task_plan.json",
    "flow": "daily",
    "repeat": 1,
    "dry_run": False,
    # None = 自动（源码运行开、打包版关）；true/false 强制开关
    "dev_mode": None,
    # 界面入口开关：visible=是否显示，enabled=是否可用（见 core/features.py）
    "features": copy.deepcopy(FEATURE_DEFAULTS),
    "adb": {
        "adb_path": "",
        "device": "127.0.0.1:7555",
        "resolution": "",
    },
    "screen": {
        "window_title": "这城有良田",
        "capture_mode": "mss",
        "region": "",
        "follow_window": True,
    },
    "variables": {"redeem_code": ""},
    # 循环监控（方案 A：主流程空闲时按间隔巡检，命中就跑指定任务）
    "watchers": {
        "enabled": False,
        "rules": [
            {
                "key": "redPacket",
                "name": "红包",
                "enabled": True,
                "interval_seconds": 300,
                "task": "claimRedPacket",
                "template": "templates/event/red_packet.png",
                "threshold": 0.85,
                "confirm_frames": 2,
                "cooldown_seconds": 600,
                "max_per_hour": 6,
                "region": None,
                "jitter_seconds": 5,
                "grayscale": True,
                "multiscale": False,
            }
        ],
    },
    # 兼容旧配置：点位勾选曾存在这里，现在由 task_plan.json 的 points 接管
    "selection": {},
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Optional[str] = None) -> dict:
    path = os.path.abspath(path or os.path.join(os.getcwd(), "config.json"))
    if not os.path.isfile(path):
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return copy.deepcopy(DEFAULT_CONFIG)
        return _merge(DEFAULT_CONFIG, data)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict, path: Optional[str] = None) -> bool:
    path = os.path.abspath(path or os.path.join(os.getcwd(), "config.json"))
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(config, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        return True
    except OSError:
        return False
