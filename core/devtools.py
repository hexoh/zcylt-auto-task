"""开发者工具开关。

「任务添加器」是给配置作者用的开发工具：它负责把坐标/模板录成
``tasks/*.json``。普通用户只需要一份能跑的配置，看到这个入口反而会乱点，
所以发布（打包）版本里必须隐藏。

判定顺序（前者优先）：

1. 环境变量 ``ZCYL_DEV``：``1/true/on`` 强制开，``0/false/off`` 强制关
2. ``config.json`` 里的 ``dev_mode``（true / false）
3. 默认值：源码运行 = True，打包后（``sys.frozen``）= False

命令行工具（``python main.py adder``，以及兼容别名 ``picker|builder``）
不受此开关影响，因为拿到命令行的人本来就是这个程序的使用者/维护者。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

TRUE_WORDS = {"1", "true", "yes", "on", "dev", "develop"}
FALSE_WORDS = {"0", "false", "no", "off", "release", "prod"}


def is_frozen() -> bool:
    """是否运行在打包后的可执行文件里（PyInstaller / Nuitka 都会设 frozen）。"""
    return bool(getattr(sys, "frozen", False)) or bool(
        getattr(sys, "_MEIPASS", None)
    )


def _env_override() -> Optional[bool]:
    raw = (os.environ.get("ZCYL_DEV") or "").strip().lower()
    if raw in TRUE_WORDS:
        return True
    if raw in FALSE_WORDS:
        return False
    return None


def resolve_dev_mode(config: Optional[dict] = None) -> bool:
    """返回是否启用开发者工具。"""
    env = _env_override()
    if env is not None:
        return env
    if isinstance(config, dict) and isinstance(config.get("dev_mode"), bool):
        return config["dev_mode"]
    return not is_frozen()


def dev_mode_source(config: Optional[dict] = None) -> str:
    """给界面/日志显示的一句话说明（排查「为什么菜单不见」时很有用）。"""
    if _env_override() is not None:
        return f"环境变量 ZCYL_DEV={os.environ.get('ZCYL_DEV')}"
    if isinstance(config, dict) and isinstance(config.get("dev_mode"), bool):
        return f"config.json dev_mode={config['dev_mode']}"
    return "打包版本自动隐藏" if is_frozen() else "源码运行（默认开启）"


def assert_dev_mode(config: Optional[dict] = None, action: str = "该功能") -> None:
    """在开发工具入口做一次断言，避免发布版被命令行外的路径打开。"""
    if not resolve_dev_mode(config):
        raise PermissionError(
            f"{action}仅在开发者模式下可用（{dev_mode_source(config)}）；"
            "如需临时开启，设置环境变量 ZCYL_DEV=1 或在 config.json 写 \"dev_mode\": true"
        )
