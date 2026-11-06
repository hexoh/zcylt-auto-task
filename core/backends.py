"""操作后端：把「截图 / 点击 / 滑动 / 输入 / 按键」抽象成统一接口。

两个实现：

- :class:`AdbBackend`    Android 模拟器（MuMu / 蓝叠 / 夜神），走 ``adb``
- :class:`ScreenBackend` 微信小程序 PC 端，走窗口截图 + 鼠标键盘模拟
- :class:`MockBackend`   演练模式，不碰硬件，用一张拼贴画布模拟画面

上层（runner）只管调用 ``tap(x, y, coord="rel1000")``，坐标换算由各后端按自己的
:class:`~core.coords.Geometry` 完成。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Sequence, Tuple

from . import vision
from .coords import Geometry, to_abs
from .errors import BackendError

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


# --------------------------------------------------------------------------- 基类
class Backend(ABC):
    """操作后端基类。"""

    name = "base"
    label = "未知后端"

    def __init__(self, config: Optional[dict] = None, logger=None):
        self.config = dict(config or {})
        self.logger = logger
        self._geom: Optional[Geometry] = None

    # ---------------------------------------------------------------- 生命周期
    @abstractmethod
    def connect(self) -> None:
        """建立连接 / 定位窗口，成功后 ``self._geom`` 必须可用。"""

    def close(self) -> None:
        """释放资源，默认什么都不做。"""

    # ---------------------------------------------------------------- 基本信息
    @property
    def geometry(self) -> Geometry:
        if self._geom is None:
            raise BackendError("后端尚未连接，无法获取画面尺寸")
        return self._geom

    @property
    def screen_size(self) -> Tuple[int, int]:
        g = self.geometry
        return (g.width, g.height)

    def describe(self) -> str:
        return f"{self.label} {self.geometry}"

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    # ---------------------------------------------------------------- 操作
    @abstractmethod
    def screenshot(self):
        """返回 BGR 的 numpy 数组（整块画面）。"""

    def tap(self, x: float, y: float, coord: str = "rel1000") -> Tuple[int, int]:
        ax, ay = to_abs(x, y, coord, self.geometry)
        self._tap_abs(ax, ay)
        return (ax, ay)

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        coord: str = "rel1000",
        duration_ms: int = 300,
    ) -> None:
        g = self.geometry
        ax1, ay1 = to_abs(x1, y1, coord, g)
        ax2, ay2 = to_abs(x2, y2, coord, g)
        self._swipe_abs(ax1, ay1, ax2, ay2, int(duration_ms))

    @abstractmethod
    def _tap_abs(self, x: int, y: int) -> None: ...

    @abstractmethod
    def _swipe_abs(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None: ...

    @abstractmethod
    def input_text(self, text: str) -> None: ...

    @abstractmethod
    def key(self, name: str) -> None: ...

    def launch(self, package: str) -> None:
        raise BackendError(f"{self.label} 不支持启动应用")

    def kill(self, package: str) -> None:
        raise BackendError(f"{self.label} 不支持关闭应用")


# --------------------------------------------------------------------------- ADB
KEYCODES = {
    "back": 4,
    "home": 3,
    "enter": 66,
    "menu": 82,
    "power": 26,
    "volume_up": 24,
    "volume_down": 25,
    "delete": 67,
    "tab": 61,
    "space": 62,
    "recent": 187,
    "esc": 111,
}

#: adb shell input text 需要转义的字符
_ADB_TEXT_SPECIAL = set("\\\"'`$&<>|;()*?#[]{}~!^ ")


def _escape_adb_text(text: str) -> str:
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in _ADB_TEXT_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class AdbBackend(Backend):
    """通过 ADB 驱动安卓模拟器。"""

    name = "adb"
    label = "ADB"

    def __init__(self, config: Optional[dict] = None, logger=None):
        super().__init__(config, logger)
        self.adb_path = str(self.config.get("adb_path") or "").strip() or "adb"
        self.device = str(self.config.get("device") or "").strip() or None
        self.screencap_timeout = float(self.config.get("screencap_timeout") or 20)
        self._configured_size = self._parse_resolution(self.config.get("resolution"))
        self._lock = threading.Lock()
        self._last_frame = None

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _parse_resolution(value) -> Optional[Tuple[int, int]]:
        if not value:
            return None
        if isinstance(value, (tuple, list)) and len(value) == 2:
            try:
                w, h = int(value[0]), int(value[1])
                return (w, h) if w > 0 and h > 0 else None
            except (TypeError, ValueError):
                return None
        m = re.match(r"^\s*(\d+)\s*[xX*×,\s]\s*(\d+)\s*$", str(value))
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        return (w, h) if w > 0 and h > 0 else None

    def _resolve_adb(self) -> str:
        path = self.adb_path
        if os.path.isfile(path):
            return path
        found = shutil.which(path)
        if found:
            return found
        # 常见模拟器自带 adb 位置（Windows）
        if IS_WIN:
            guesses = [
                r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
                r"C:\Program Files\Netease\MuMuPlayer\shell\adb.exe",
                r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
                r"C:\Program Files (x86)\Nox\bin\nox_adb.exe",
            ]
            for g in guesses:
                if os.path.isfile(g):
                    self._log(f"自动找到 adb：{g}", "warn")
                    return g
        raise BackendError(
            f"找不到 adb 可执行文件：{self.adb_path}\n"
            "请安装 Android Platform Tools，或在设置里填写 adb 完整路径。"
        )

    def _cmd(self, args: Sequence[str], with_device: bool = True) -> List[str]:
        cmd = [self.adb_path]
        if with_device and self.device:
            cmd += ["-s", self.device]
        cmd += list(args)
        return cmd

    def _run(
        self,
        args: Sequence[str],
        timeout: Optional[float] = None,
        with_device: bool = True,
        binary: bool = False,
        check: bool = True,
    ):
        cmd = self._cmd(args, with_device=with_device)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout or 30,
                text=not binary,
            )
        except FileNotFoundError as exc:
            raise BackendError(f"adb 无法执行：{cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"adb 命令超时（{timeout}s）：{' '.join(args)}") from exc

        if check and proc.returncode != 0:
            detail = proc.stderr
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise BackendError(f"adb 命令失败：{' '.join(args)}\n{(detail or '').strip()}")
        return proc.stdout

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        self.adb_path = self._resolve_adb()
        self._run(["start-server"], with_device=False, timeout=40, check=False)

        if self.device and ":" in self.device:
            out = self._run(["connect", self.device], with_device=False, timeout=20, check=False)
            self._log(f"adb connect {self.device}: {str(out).strip()}", "debug")

        devices = self._list_devices()
        if not devices:
            raise BackendError(
                "没有检测到安卓设备。\n"
                "1. 确认模拟器已启动\n"
                "2. 模拟器需开启 ADB 调试\n"
                "3. 或在设置里填写设备地址（如 127.0.0.1:7555）"
            )
        if self.device:
            if self.device not in devices:
                raise BackendError(
                    f"设备 {self.device} 不在线，当前在线设备：{', '.join(devices) or '无'}"
                )
        else:
            self.device = devices[0]
            self._log(f"未指定设备，自动使用 {self.device}", "warn")

        if self._configured_size:
            w, h = self._configured_size
            self._geom = Geometry(0, 0, w, h)
            self._log(f"使用配置分辨率 {self._geom}", "debug")
        else:
            self._geom = Geometry(0, 0, *self._query_size())

        self._log(f"ADB 已连接：{self.device} 分辨率 {self.screen_size[0]}x{self.screen_size[1]}")

    def _list_devices(self) -> List[str]:
        out = self._run(["devices"], with_device=False, timeout=20)
        devices: List[str] = []
        for line in str(out).splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def _query_size(self) -> Tuple[int, int]:
        try:
            out = str(self._run(["shell", "wm", "size"], timeout=15))
        except BackendError as exc:
            self._log(f"获取分辨率失败，使用 1280x720 兜底：{exc}", "warn")
            return (1280, 720)
        override = re.search(r"Override size:\s*(\d+)x(\d+)", out)
        physical = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        m = override or physical
        if not m:
            self._log(f"无法解析 wm size 输出：{out.strip()!r}，使用 1280x720", "warn")
            return (1280, 720)
        return (int(m.group(1)), int(m.group(2)))

    def verify(self) -> None:
        """连通性自检。"""
        if not self.device:
            raise BackendError("尚未连接设备")

    # ---------------------------------------------------------------- 操作
    def screenshot(self):
        if not vision.CV_AVAILABLE:
            raise BackendError("缺少 opencv-python / numpy，无法截图解码")

        with self._lock:
            data = self._run(
                ["exec-out", "screencap", "-p"], timeout=self.screencap_timeout, binary=True
            )
            frame = self._decode(data)
            if frame is None:
                # Windows 上 exec-out 可能被 CRLF 破坏，走 pull 兜底
                frame = self._screenshot_via_file()
            if frame is None:
                raise BackendError("截图解码失败（adb screencap 输出损坏）")
            self._last_frame = frame
            h, w = frame.shape[:2]
            if w > 0 and h > 0 and (w, h) != self.screen_size:
                if self._configured_size != (w, h):
                    self._log(f"截图尺寸 {w}x{h} 与配置不一致，已按截图尺寸校正", "debug")
                self._geom = Geometry(0, 0, w, h)
            return frame

    def _screenshot_via_file(self):
        remote = "/sdcard/.zcylt_cap.png"
        try:
            self._run(["shell", "screencap", "-p", remote], timeout=self.screencap_timeout)
            data = self._run(["exec-out", "cat", remote], timeout=self.screencap_timeout, binary=True)
            frame = self._decode(data)
            self._run(["shell", "rm", "-f", remote], timeout=10, check=False)
            return frame
        except BackendError:
            return None

    @staticmethod
    def _decode(data):
        if not data:
            return None
        try:
            arr = vision.np.frombuffer(data, dtype=vision.np.uint8)
            return vision.cv2.imdecode(arr, vision.cv2.IMREAD_COLOR)
        except Exception:
            return None

    def _tap_abs(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)], timeout=15)

    def _swipe_abs(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._run(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(max(1, duration_ms))],
            timeout=20,
        )

    def input_text(self, text: str) -> None:
        if not text:
            return
        escaped = _escape_adb_text(text)
        if not escaped.isascii():
            self._log("ADB input text 对非 ASCII 字符支持有限，可能输入失败", "warn")
        self._run(["shell", "input", "text", escaped], timeout=20)

    def key(self, name: str) -> None:
        code = KEYCODES.get(str(name).strip().lower())
        if code is None:
            raise BackendError(f"未知按键 {name!r}，支持：{', '.join(KEYCODES)}")
        self._run(["shell", "input", "keyevent", str(code)], timeout=15)

    def launch(self, package: str) -> None:
        self._run(
            [
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=30,
        )

    def kill(self, package: str) -> None:
        self._run(["shell", "am", "force-stop", package], timeout=20)

    def describe(self) -> str:
        dev = self.device or "(未连接)"
        return f"ADB {dev} {self.geometry}"


# --------------------------------------------------------------------------- 屏幕
#: 屏幕模式下的按键映射（PC 端按键语义）
SCREEN_KEYS = {
    "back": "esc",
    "esc": "esc",
    "home": "home",
    "enter": "enter",
    "tab": "tab",
    "space": "space",
    "delete": "delete",
    "pageup": "pageup",
    "pagedown": "pagedown",
}


class ScreenBackend(Backend):
    """微信小程序 PC 端：截窗口 + 模拟鼠标键盘。"""

    name = "screen"
    label = "屏幕"

    def __init__(self, config: Optional[dict] = None, logger=None):
        super().__init__(config, logger)
        self.window_title = str(self.config.get("window_title") or "").strip()
        self.capture_mode = str(self.config.get("capture_mode") or "mss").lower()
        self.region = self.config.get("region")  # [x, y, w, h] 绝对屏幕坐标
        self.follow_window = bool(self.config.get("follow_window", True))
        self._win = None
        self._pyautogui = None
        self._last_geom_check = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        self._load_gui_deps()

        if self.region:
            try:
                self._geom = Geometry.from_sequence(self.region)
            except Exception as exc:
                raise BackendError(f"区域配置无效：{self.region!r}（{exc}）") from exc
            self._log(f"使用配置区域 {self._geom}")
            return

        if self.window_title:
            geom = self._locate_window(activate=True)
            if geom:
                self._geom = geom
                self._log(f"已锁定窗口「{self.window_title}」{geom}")
                return
            self._log(
                f"没找到标题包含「{self.window_title}」的窗口，退回全屏模式；"
                "建议用「任务添加器」重新确认区域",
                "warn",
            )

        self._geom = self._full_screen_geometry()
        self._log(f"使用全屏区域 {self._geom}")

    def _load_gui_deps(self) -> None:
        try:
            import pyautogui
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise BackendError(
                "缺少 pyautogui，无法模拟鼠标键盘。请执行：pip install pyautogui"
            ) from exc
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        self._pyautogui = pyautogui

    def _locate_window(self, activate: bool = False) -> Optional[Geometry]:
        try:
            import pygetwindow as gw
        except Exception:
            self._log("未安装 pygetwindow，无法按窗口标题定位，请用区域配置", "warn")
            return None
        try:
            wins = [w for w in gw.getWindowsWithTitle(self.window_title) if w.width > 0 and w.height > 0]
        except Exception as exc:
            self._log(f"枚举窗口失败：{exc}", "warn")
            return None
        if not wins:
            return None
        win = wins[0]
        self._win = win
        if activate:
            try:
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.activate()
                time.sleep(0.2)
            except Exception as exc:
                self._log(f"窗口置前失败（可忽略）：{exc}", "debug")
        try:
            return Geometry(int(win.left), int(win.top), int(win.width), int(win.height))
        except Exception:
            return None

    def _full_screen_geometry(self) -> Geometry:
        try:
            import mss

            with mss.mss() as sct:
                mon = sct.monitors[1]
                return Geometry(int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"]))
        except Exception:
            try:
                from PIL import ImageGrab

                img = ImageGrab.grab()
                return Geometry(0, 0, img.width, img.height)
            except Exception as exc:
                raise BackendError(f"无法获取屏幕尺寸：{exc}") from exc

    def _maybe_refresh_geometry(self) -> None:
        """窗口可能被拖动，定期校准（3 秒节流）。"""
        if not (self.follow_window and self.window_title and not self.region):
            return
        now = time.time()
        if now - self._last_geom_check < 3.0:
            return
        self._last_geom_check = now
        geom = self._locate_window()
        if geom and geom != self._geom:
            self._log(f"窗口位置变化：{self._geom} → {geom}", "debug")
            self._geom = geom

    # ---------------------------------------------------------------- 截图
    def screenshot(self):
        if not vision.CV_AVAILABLE:
            raise BackendError("缺少 opencv-python / numpy，无法处理截图")
        self._maybe_refresh_geometry()
        g = self.geometry
        with self._lock:
            if self.capture_mode == "pil":
                frame = self._grab_pil(g)
            else:
                frame = self._grab_mss(g)
        if frame is None:
            raise BackendError("截图为空，可能是屏幕录制权限未授权（macOS：系统设置 → 隐私与安全性 → 屏幕录制）")
        self._geom = Geometry(g.x, g.y, frame.shape[1], frame.shape[0])
        return frame

    def _grab_mss(self, g: Geometry):
        try:
            import mss
        except Exception:
            return self._grab_pil(g)
        try:
            with mss.mss() as sct:
                raw = sct.grab(
                    {"left": g.x, "top": g.y, "width": g.width, "height": g.height}
                )
                arr = vision.np.asarray(raw)  # BGRA
                return vision.np.ascontiguousarray(arr[:, :, :3])
        except Exception as exc:
            self._log(f"mss 截图失败，尝试 PIL：{exc}", "warn")
            return self._grab_pil(g)

    def _grab_pil(self, g: Geometry):
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=g.bounds)
            rgb = vision.np.array(img.convert("RGB"))
            return vision.np.ascontiguousarray(rgb[:, :, ::-1])
        except Exception as exc:
            raise BackendError(f"截图失败：{exc}") from exc

    # ---------------------------------------------------------------- 操作
    def _tap_abs(self, x: int, y: int) -> None:
        self._pyautogui.click(x, y)
        self._ensure_foreground()

    def _swipe_abs(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        gui = self._pyautogui
        gui.moveTo(x1, y1)
        gui.dragTo(x2, y2, duration=max(0.05, duration_ms / 1000.0), button="left")

    def _ensure_foreground(self) -> None:
        """点击后若窗口失焦会点空，简单校验并尝试拉回。"""
        if self._win is None:
            return
        try:
            if not self._win.isActive:
                self._win.activate()
        except Exception:
            pass

    def input_text(self, text: str) -> None:
        if not text:
            return
        try:
            import pyperclip

            pyperclip.copy(text)
            time.sleep(0.05)
            modifier = "command" if IS_MAC else "ctrl"
            self._pyautogui.hotkey(modifier, "v")
        except Exception as exc:
            self._log(f"剪贴板粘贴失败，改用逐字输入：{exc}", "warn")
            self._pyautogui.write(text, interval=0.02)

    def key(self, name: str) -> None:
        key = SCREEN_KEYS.get(str(name).strip().lower())
        if key is None:
            raise BackendError(f"未知按键 {name!r}，支持：{', '.join(SCREEN_KEYS)}")
        self._pyautogui.press(key)

    def describe(self) -> str:
        title = self.window_title or "(整屏)"
        return f"屏幕 {title} {self.geometry}"


# --------------------------------------------------------------------------- 演练
class MockBackend(Backend):
    """演练后端：不操作真实设备，用拼贴画布让所有模板都能匹配上。

    用于在没设备时验证 JSON 流程是否连通（模板缺失仍会失败，这是正确的）。
    """

    name = "mock"
    label = "演练"

    def __init__(self, config: Optional[dict] = None, logger=None, templates_dir: Optional[str] = None):
        super().__init__(config, logger)
        size = self.config.get("size") or (1280, 720)
        self._geom = Geometry(0, 0, int(size[0]), int(size[1]))
        self.actions: List[tuple] = []
        self.templates_dir = templates_dir
        self._canvas = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        self._log(f"演练模式已启用，不产生真实点击（画布 {self.geometry}）", "warn")

    def screenshot(self):
        if not vision.CV_AVAILABLE:
            raise BackendError("演练模式需要 opencv-python / numpy")
        with self._lock:
            if self._canvas is None:
                self._canvas = self._build_canvas()
            return self._canvas.copy()

    def _build_canvas(self):
        np = vision.np
        cv2 = vision.cv2
        w, h = self.screen_size
        canvas = np.full((h, w, 3), 32, dtype=np.uint8)
        if not self.templates_dir or not os.path.isdir(self.templates_dir):
            return canvas

        files: List[str] = []
        for root, _dirs, names in os.walk(self.templates_dir):
            for n in sorted(names):
                if n.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                    files.append(os.path.join(root, n))
        if not files:
            return canvas

        cols = max(1, int(len(files) ** 0.5 + 0.5))
        rows = (len(files) + cols - 1) // cols
        cell_w, cell_h = max(1, w // cols), max(1, h // rows)
        for idx, path in enumerate(files):
            try:
                img = vision.imread(path)
            except Exception:
                continue
            ih, iw = img.shape[:2]
            scale = min((cell_w - 8) / iw, (cell_h - 8) / ih, 1.0)
            if scale <= 0:
                continue
            size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
            if size[0] != iw or size[1] != ih:
                img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            r, c = divmod(idx, cols)
            x = c * cell_w + (cell_w - size[0]) // 2
            y = r * cell_h + (cell_h - size[1]) // 2
            canvas[y : y + size[1], x : x + size[0]] = img
        return canvas

    def _record(self, kind: str, *args) -> None:
        with self._lock:
            self.actions.append((kind, *args))
        self._log(f"[演练] {kind} {args}", "debug")

    def _tap_abs(self, x: int, y: int) -> None:
        self._record("tap", x, y)

    def _swipe_abs(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._record("swipe", x1, y1, x2, y2, duration_ms)

    def input_text(self, text: str) -> None:
        self._record("text", text)

    def key(self, name: str) -> None:
        self._record("key", name)

    def launch(self, package: str) -> None:
        self._record("launch", package)

    def kill(self, package: str) -> None:
        self._record("kill", package)

    def describe(self) -> str:
        return f"演练模式 {self.geometry}"


def create_backend(mode: str, config: Optional[dict] = None, logger=None, **kwargs) -> Backend:
    """工厂：``adb`` / ``screen`` / ``mock``。"""
    mode = (mode or "adb").strip().lower()
    if mode == "adb":
        return AdbBackend(config, logger)
    if mode in ("screen", "screen_capture", "pc"):
        return ScreenBackend(config, logger)
    if mode in ("mock", "dry", "dryrun"):
        return MockBackend(config, logger, templates_dir=kwargs.get("templates_dir"))
    raise BackendError(f"未知后端模式：{mode}")
