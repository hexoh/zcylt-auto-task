"""共用截图源：把「截屏 / ADB 投屏 + 坐标原点」抽出来。

任务添加器（``tools/adder.py``）和旧版拾取器（``tools/picker.py``）都需要
同一份画面和一整套坐标换算，所以截图逻辑只留在这里一份，避免两边慢慢漂移。

约定：``capture()`` 返回 ``(frame_bgr, origin)``，``origin`` 是这块画面左上角
对应的屏幕/设备绝对坐标，算绝对坐标时统一用 ``画面内坐标 + origin``。
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple

from core import vision
from core.backends import AdbBackend, ScreenBackend
from core.errors import BackendError

Origin = Tuple[int, int]


class CaptureSource:
    """按模式取一帧画面；内部缓存后端连接，重复调用只截不重连。"""

    def __init__(self, mode: str = "screen", adb_config: Optional[dict] = None):
        self.mode = "adb" if str(mode).lower() == "adb" else "screen"
        self.adb_config = dict(adb_config or {})
        self.last_label = ""
        self._adb: Optional[AdbBackend] = None
        self._adb_key = None
        self._backend: Optional[ScreenBackend] = None
        self._screen_key = None

    # ------------------------------------------------------------------ 截图
    def capture(self, target: str = "", relocate: bool = False):
        """取一帧。``target`` 在 ADB 模式是设备地址，在屏幕模式是窗口标题。"""
        if not vision.CV_AVAILABLE:
            raise BackendError("缺少 opencv-python / numpy，请先 pip install -r requirements.txt")
        if self.mode == "adb":
            return self._capture_adb(target)
        return self._capture_screen(target, relocate=relocate)

    def _capture_adb(self, target: str):
        cfg = dict(self.adb_config)
        cfg["device"] = (target or cfg.get("device") or "").strip()
        cfg.pop("window_title", None)
        key = (cfg.get("adb_path"), cfg.get("device"), cfg.get("resolution"))
        if self._adb is None or key != self._adb_key:
            adb = AdbBackend(cfg)
            adb.connect()
            self._adb, self._adb_key = adb, key
        frame = self._adb.screenshot()
        self.last_label = self._adb.describe()
        return frame, (0, 0)

    def _capture_screen(self, target: str, relocate: bool = False):
        title = (target or "").strip()
        key = (title,)
        if self._backend is None or key != self._screen_key:
            backend = ScreenBackend(
                {"window_title": title, "capture_mode": "mss", "follow_window": True}
            )
            backend.connect()
            self._backend, self._screen_key = backend, key
        elif relocate:
            # 手动刷新时强制重新定位窗口，别用缓存的旧位置
            self._backend._last_geom_check = 0.0  # noqa: SLF001
        frame = self._backend.screenshot()
        g = self._backend.geometry
        self.last_label = self._backend.describe()
        return frame, (int(g.x), int(g.y))

    # ------------------------------------------------------------------ 操作
    def tap_abs(self, x: int, y: int) -> None:
        """按绝对坐标点一下（「测试点击」用，验证录的坐标对不对）。"""
        if self.mode == "adb":
            if self._adb is None:
                raise BackendError("ADB 尚未连接，请先刷新画面")
            self._adb.tap(x, y, "abs")
            return
        try:
            import pyautogui
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
            raise BackendError(f"屏幕模式需要 pyautogui：{exc}") from exc
        pyautogui.click(int(x), int(y))

    def close(self) -> None:
        for obj in (self._adb, self._backend):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._adb = None
        self._backend = None
        self._adb_key = None
        self._screen_key = None


def open_folder(path: str) -> None:
    """在系统文件管理器里打开目录。"""
    import os

    os.makedirs(path, exist_ok=True)
    if sys.platform == "darwin":
        os.system(f'open "{path}"')
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        os.system(f'xdg-open "{path}"')
