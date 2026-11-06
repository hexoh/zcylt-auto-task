"""坐标换算。

三套坐标系：

- ``abs``     绝对像素坐标（模拟器的设备像素 / 桌面屏幕像素）
- ``window``  相对窗口左上角的像素坐标（窗口内部坐标）
- ``rel1000`` 相对坐标，0-1000 映射到宽高（JSON 里推荐用这一套，换分辨率不会挂）

后端（ADB / 屏幕截取）各自持有一个 :class:`Geometry`，把上层传下来的任意坐标系
统一换算成 ``abs`` 再执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

REL_BASE = 1000

#: 支持的坐标类型
COORDS = ("rel1000", "window", "abs")


def _clamp(value: int, low: int, high: int) -> int:
    if high < low:
        low, high = high, low
    return max(low, min(high, int(value)))


@dataclass(frozen=True)
class Geometry:
    """一块矩形区域：桌面上的窗口，或者模拟器画面。"""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def __post_init__(self):
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"无效的区域尺寸: {self.width}x{self.height}")

    @property
    def bounds(self) -> Tuple[int, int, int, int]:
        """(left, top, right, bottom)，可直接给 mss / PIL 用。"""
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def shifted(self, dx: int, dy: int) -> "Geometry":
        return Geometry(self.x + dx, self.y + dy, self.width, self.height)

    @classmethod
    def from_sequence(cls, seq: Sequence[int]) -> "Geometry":
        if len(seq) != 4:
            raise ValueError(f"区域需要 4 个值 (x, y, w, h)，收到 {seq!r}")
        return cls(*(int(v) for v in seq))

    def __str__(self) -> str:
        if self.x == 0 and self.y == 0:
            return f"{self.width}x{self.height}"
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def rel1000_to_px(x: float, y: float, width: int, height: int) -> Tuple[int, int]:
    """rel1000 → 像素（相对同一块区域）。"""
    if width <= 0 or height <= 0:
        raise ValueError(f"无效的区域尺寸: {width}x{height}")
    px = int(round(float(x) / REL_BASE * width))
    py = int(round(float(y) / REL_BASE * height))
    return (_clamp(px, 0, width - 1), _clamp(py, 0, height - 1))


def px_to_rel1000(x: float, y: float, width: int, height: int) -> Tuple[int, int]:
    """像素（相对同一块区域）→ rel1000。"""
    if width <= 0 or height <= 0:
        raise ValueError(f"无效的区域尺寸: {width}x{height}")
    rx = int(round(float(x) / width * REL_BASE))
    ry = int(round(float(y) / height * REL_BASE))
    return (_clamp(rx, 0, REL_BASE), _clamp(ry, 0, REL_BASE))


def window_rel_to_abs(x: float, y: float, geom: Geometry) -> Tuple[int, int]:
    return (_clamp(geom.x + x, 0, geom.x + geom.width - 1), _clamp(geom.y + y, 0, geom.y + geom.height - 1))


def abs_to_window_rel(x: float, y: float, geom: Geometry) -> Tuple[int, int]:
    return (_clamp(x - geom.x, 0, geom.width - 1), _clamp(y - geom.y, 0, geom.height - 1))


def rel1000_to_abs(x: float, y: float, geom: Geometry) -> Tuple[int, int]:
    wx, wy = rel1000_to_px(x, y, geom.width, geom.height)
    return (geom.x + wx, geom.y + wy)


def abs_to_rel1000(x: float, y: float, geom: Geometry) -> Tuple[int, int]:
    wx, wy = abs_to_window_rel(x, y, geom)
    return px_to_rel1000(wx, wy, geom.width, geom.height)


def to_abs(x: float, y: float, coord: str, geom: Geometry) -> Tuple[int, int]:
    """把任意坐标系换算成绝对像素。"""
    coord = normalize_coord(coord)
    if coord == "abs":
        return (_clamp(x, 0, geom.x + geom.width - 1), _clamp(y, 0, geom.y + geom.height - 1))
    if coord == "window":
        return window_rel_to_abs(x, y, geom)
    return rel1000_to_abs(x, y, geom)


def to_window_rel(x: float, y: float, coord: str, geom: Geometry) -> Tuple[int, int]:
    ax, ay = to_abs(x, y, coord, geom)
    return abs_to_window_rel(ax, ay, geom)


def to_rel1000(x: float, y: float, coord: str, geom: Geometry) -> Tuple[int, int]:
    ax, ay = to_abs(x, y, coord, geom)
    return abs_to_rel1000(ax, ay, geom)


def normalize_coord(coord: str | None) -> str:
    """容错：``None`` / ``px`` / ``frame`` 都归一到三种标准写法。"""
    if not coord:
        return "rel1000"
    value = str(coord).strip().lower()
    aliases = {
        "rel": "rel1000",
        "rel1000": "rel1000",
        "ratio": "rel1000",
        "1000": "rel1000",
        "px": "window",
        "pixel": "window",
        "frame": "window",
        "window": "window",
        "local": "window",
        "abs": "abs",
        "absolute": "abs",
        "screen": "abs",
        "device": "abs",
    }
    if value not in aliases:
        raise ValueError(f"未知坐标类型 {coord!r}，可选：{', '.join(COORDS)}")
    return aliases[value]


def step_point(step: dict, prefix: str = "", default_coord: str = "rel1000") -> Tuple[float, float, str]:
    """从 step 里取点。``prefix="1"`` 时读 x1/y1。"""
    x_key, y_key = f"x{prefix}", f"y{prefix}"
    if x_key not in step or y_key not in step:
        raise ValueError(f"缺少坐标字段 {x_key}/{y_key}")
    coord = step.get("coord") or default_coord
    return float(step[x_key]), float(step[y_key]), coord


def format_point(x: float, y: float, coord: str) -> str:
    return f"({x:.0f}, {y:.0f}) {coord}"
