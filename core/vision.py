"""OpenCV 模板匹配。

只做一件事：在一张截图里找到模板图的位置，返回置信度和中心点。
支持灰度、阈值、多尺度、局部区域匹配，以及模板缓存。

模板图必须来自同一路截图（ADB 模式必须用 ``screencap`` 原图），
用 PC 截屏再裁剪会导致阈值必挂。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .errors import TemplateMissing, VisionUnavailable

try:  # 允许 UI 在没装 opencv 的机器上启动
    import cv2
    import numpy as np

    CV_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时的降级路径
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    CV_AVAILABLE = False


TemplateSource = Union[str, "np.ndarray"]

#: 默认多尺度系数（模板缩放比例）
DEFAULT_SCALES: Tuple[float, ...] = (0.75, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.35)


@dataclass
class Match:
    """一次匹配结果，坐标是整帧的像素坐标。"""

    score: float
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0
    template: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def __str__(self) -> str:
        cx, cy = self.center
        name = os.path.basename(self.template) if self.template else "?"
        return f"{name} score={self.score:.3f} center=({cx},{cy})"


def require_cv() -> None:
    if not CV_AVAILABLE:
        raise VisionUnavailable(
            "缺少 opencv-python / numpy，无法识图。请执行：pip install opencv-python numpy"
        )


def imread(path: str) -> "np.ndarray":
    """读图，兼容中文/空格路径（cv2.imread 对非 ASCII 路径不友好）。"""
    require_cv()
    if not os.path.isfile(path):
        raise TemplateMissing(f"模板图不存在：{path}")
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise TemplateMissing(f"模板图无法解码（可能已损坏）：{path}")
    return img


def to_gray(img: "np.ndarray") -> "np.ndarray":
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


class TemplateMatcher:
    """带缓存的模板匹配器（线程安全）。"""

    def __init__(
        self,
        threshold: float = 0.85,
        grayscale: bool = True,
        multiscale: bool = False,
        scales: Sequence[float] = DEFAULT_SCALES,
    ):
        self.threshold = float(threshold)
        self.grayscale = bool(grayscale)
        self.multiscale = bool(multiscale)
        self.scales = tuple(scales)
        self._cache: Dict[str, Tuple[float, "np.ndarray"]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 模板
    def load(self, source: TemplateSource) -> "np.ndarray":
        if not isinstance(source, str):
            return source  # 已经是 ndarray
        require_cv()
        path = os.path.abspath(source)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            raise TemplateMissing(f"模板图不存在：{path}")

        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
        img = imread(path)
        with self._lock:
            self._cache[path] = (mtime, img)
        return img

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------ 匹配
    def find(
        self,
        frame: "np.ndarray",
        template: TemplateSource,
        threshold: Optional[float] = None,
        region: Optional[Sequence[int]] = None,
        multiscale: Optional[bool] = None,
        scales: Optional[Sequence[float]] = None,
        grayscale: Optional[bool] = None,
    ) -> Optional[Match]:
        """返回最佳匹配（低于阈值返回 None）。"""
        require_cv()
        if frame is None or getattr(frame, "size", 0) == 0:
            return None

        thr = self.threshold if threshold is None else float(threshold)
        use_gray = self.grayscale if grayscale is None else bool(grayscale)
        use_multi = self.multiscale if multiscale is None else bool(multiscale)
        use_scales = tuple(scales) if scales else self.scales

        # 局部区域
        off_x, off_y = 0, 0
        search = frame
        if region:
            rx, ry, rw, rh = (int(v) for v in region)
            fh, fw = frame.shape[:2]
            rx = max(0, min(rx, fw - 1))
            ry = max(0, min(ry, fh - 1))
            rw = max(1, min(rw, fw - rx))
            rh = max(1, min(rh, fh - ry))
            search = frame[ry : ry + rh, rx : rx + rw]
            off_x, off_y = rx, ry

        tpl = self.load(template)
        name = template if isinstance(template, str) else "<ndarray>"

        if use_gray:
            search_img = to_gray(search)
            tpl_img = to_gray(tpl)
        else:
            search_img = search
            tpl_img = tpl

        sh, sw = search_img.shape[:2]
        th, tw = tpl_img.shape[:2]
        if tw < 2 or th < 2 or th > sh or tw > sw:
            return None

        best: Optional[Match] = None
        candidates = use_scales if use_multi else (1.0,)
        for scale in candidates:
            cur = tpl_img
            if abs(scale - 1.0) > 1e-6:
                nw, nh = int(round(tw * scale)), int(round(th * scale))
                if nw < 2 or nh < 2 or nh > sh or nw > sw:
                    continue
                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                cur = cv2.resize(tpl_img, (nw, nh), interpolation=interp)
            else:
                nw, nh = tw, th

            res = cv2.matchTemplate(search_img, cur, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best.score:
                best = Match(
                    score=float(max_val),
                    x=int(max_loc[0]) + off_x,
                    y=int(max_loc[1]) + off_y,
                    width=nw,
                    height=nh,
                    scale=float(scale),
                    template=name,
                )
            if max_val >= 0.995:  # 几乎完美，没必要再试其他尺度
                break

        if best is None or best.score < thr:
            return None
        return best

    def find_all(
        self,
        frame: "np.ndarray",
        template: TemplateSource,
        threshold: Optional[float] = None,
        max_results: int = 20,
        min_distance: int = 20,
        region: Optional[Sequence[int]] = None,
    ) -> List[Match]:
        """找出所有高于阈值的位置（非极大值抑制）。"""
        require_cv()
        thr = self.threshold if threshold is None else float(threshold)
        search = frame
        off_x = off_y = 0
        if region:
            rx, ry, rw, rh = (int(v) for v in region)
            search = frame[ry : ry + rh, rx : rx + rw]
            off_x, off_y = rx, ry

        tpl = self.load(template)
        search_img = to_gray(search) if self.grayscale else search
        tpl_img = to_gray(tpl) if self.grayscale else tpl
        sh, sw = search_img.shape[:2]
        th, tw = tpl_img.shape[:2]
        if th > sh or tw > sw:
            return []

        res = cv2.matchTemplate(search_img, tpl_img, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= thr)
        scored = sorted(
            ((float(res[y, x]), int(x), int(y)) for y, x in zip(ys, xs)), reverse=True
        )

        picked: List[Match] = []
        for score, x, y in scored:
            if len(picked) >= max_results:
                break
            cx, cy = x + tw // 2, y + th // 2
            if any(
                (m.x + m.width // 2 - cx) ** 2 + (m.y + m.height // 2 - cy) ** 2
                < min_distance**2
                for m in picked
            ):
                continue
            picked.append(
                Match(score, x + off_x, y + off_y, tw, th, 1.0, str(template))
            )
        return picked

    def find_best(
        self,
        frame: "np.ndarray",
        templates: Iterable[TemplateSource],
        threshold: Optional[float] = None,
        **kwargs,
    ) -> Optional[Match]:
        """在多个模板里取分数最高的那个（用于「任意一个按钮都能点」的场景）。"""
        best = None
        for tpl in templates:
            m = self.find(frame, tpl, threshold=threshold, **kwargs)
            if m and (best is None or m.score > best.score):
                best = m
        return best

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def signature(frame: "np.ndarray") -> str:
        """截图指纹，用于判断画面是否变化（等转场用）。"""
        require_cv()
        if frame is None:
            return ""
        small = cv2.resize(to_gray(frame), (32, 18), interpolation=cv2.INTER_AREA)
        return hashlib.md5(small.tobytes()).hexdigest()


def draw_match(frame: "np.ndarray", match: Match, color=(0, 0, 255)) -> "np.ndarray":
    """调试用：把匹配框画在截图上。"""
    require_cv()
    out = frame.copy()
    x, y, w, h = match.rect
    cv2.rectangle(out, (x, y), (x + w, y + h), color, 3)
    return out
