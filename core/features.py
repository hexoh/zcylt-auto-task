"""界面入口开关：哪些方案 / 工具出现在界面上，出现了能不能用。

配置写在 ``config.json`` 的 ``features`` 段（缺省值见 :data:`FEATURE_DEFAULTS`）。
两个开关的语义刻意分开：

``visible = false``
    入口**不渲染**——界面上完全看不到，适合「当前版本不开放」。

``visible = true, enabled = false``
    入口照常渲染但**置灰不可用**，点一下会说明「为什么不可用、怎么打开」。
    适合「功能已写完但不打算现在开放」，比凭空消失更好排查。

默认只开放模拟器（ADB），屏幕截取保留为「可见但未启用」。
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

# 顺序即界面上的展示顺序
MODE_ORDER = ("adb", "screen")
TOOL_ORDER = ("reload", "templates", "logs", "adder")

FEATURE_DEFAULTS: Dict[str, dict] = {
    "modes": {
        "adb": {
            "label": "模拟器",
            "hint": "Android ADB",
            "visible": True,
            "enabled": True,
        },
        "screen": {
            "label": "屏幕截取",
            "hint": "截屏 + 模拟键鼠",
            "visible": True,
            "enabled": False,
        },
    },
    "tools": {
        "reload": {"label": "重载任务配置", "visible": True, "enabled": True},
        "templates": {"label": "打开模板目录", "visible": True, "enabled": True},
        "logs": {"label": "打开日志目录", "visible": True, "enabled": True},
        "adder": {"label": "任务添加器（开发者）", "visible": True, "enabled": True},
    },
    "settings": {"label": "设置", "visible": True, "enabled": True},
}

# 入口被关掉时给用户看的说明，避免「找不到入口」的困惑
_LOCATION = {
    ("modes", "adb"): 'config.json → features.modes.adb',
    ("modes", "screen"): 'config.json → features.modes.screen',
    ("settings", ""): 'config.json → features.settings',
    ("tools", "reload"): 'config.json → features.tools.reload',
    ("tools", "templates"): 'config.json → features.tools.templates',
    ("tools", "logs"): 'config.json → features.tools.logs',
    ("tools", "adder"): 'config.json → features.tools.adder',
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def feature_config(config: Optional[dict] = None) -> dict:
    """config.json → 合并缺省值后的 features 树。"""
    raw = (config or {}).get("features")
    if not isinstance(raw, dict):
        raw = {}
    return _merge(FEATURE_DEFAULTS, raw)


def feature(config: Optional[dict], group: str, key: str = "") -> dict:
    """取单个入口的 ``{label, visible, enabled, hint}``。"""
    tree = feature_config(config)
    if key:
        entry = (tree.get(group) or {}).get(key)
    else:
        entry = tree.get(group)
    if not isinstance(entry, dict):
        entry = {}
    return {
        "label": str(entry.get("label") or key or group),
        "hint": str(entry.get("hint") or ""),
        "visible": bool(entry.get("visible", True)),
        "enabled": bool(entry.get("enabled", True)),
    }


def is_visible(config: Optional[dict], group: str, key: str = "") -> bool:
    """入口是否需要渲染（只看 visible，不看 enabled）。"""
    return feature(config, group, key)["visible"]


def is_enabled(config: Optional[dict], group: str, key: str = "") -> bool:
    return feature(config, group, key)["enabled"]


def modes(config: Optional[dict]) -> List[dict]:
    """界面上要渲染的方案入口（按固定顺序，只返回 visible 的）。"""
    result: List[dict] = []
    for key in MODE_ORDER:
        item = feature(config, "modes", key)
        if not item["visible"]:
            continue
        result.append({"key": key, **item})
    return result


def resolve_mode(config: Optional[dict], wanted: Optional[str]) -> Optional[str]:
    """挑一个可用方案：优先 ``wanted``，其次任意 enabled 的方案。

    返回 ``None`` 表示没有任何方案可用（界面需要给出提示）。
    """
    tree = feature_config(config)
    available = [
        key
        for key in MODE_ORDER
        if isinstance((tree.get("modes") or {}).get(key), dict)
        and feature(config, "modes", key)["visible"]
        and feature(config, "modes", key)["enabled"]
    ]
    if wanted in available:
        return wanted
    return available[0] if available else None


def disabled_reason(config: Optional[dict], group: str, key: str = "") -> str:
    """「这个入口为什么点不动」的一句话说明。"""
    item = feature(config, group, key)
    where = _LOCATION.get((group, key), f"config.json → features.{group}")
    if not item["visible"]:
        return (
            f"「{item['label']}」当前被隐藏（visible=false）。\n"
            f"把 {where}.visible 改成 true 后重启界面即可。"
        )
    return (
        f"「{item['label']}」当前未启用。\n"
        f"把 {where}.enabled 改成 true 后重启界面即可。"
    )
