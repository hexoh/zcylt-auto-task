"""坐标拾取器 + 模板截图工具。

两种模式：

- **屏幕截取**（微信小程序 PC 端）：截取窗口区域或整屏，鼠标在图上点选 / 框选
- **ADB 投屏**（模拟器）：定时 ``screencap`` 刷新，点选后自动换算回设备像素

输出规范：JSON 只存 rel1000 相对坐标 + 模板相对路径，绝对像素只在界面上显示。

用法::

    python -m tools.picker            # 独立窗口
    python main.py picker             # 从主程序打开
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageTk

# 允许 python tools/picker.py 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backends import AdbBackend  # noqa: E402
from core.coords import px_to_rel1000  # noqa: E402
from core import vision  # noqa: E402
from tools.capture import CaptureSource, open_folder  # noqa: E402

CANVAS_W, CANVAS_H = 760, 520
BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sanitize_name(name: str) -> str:
    name = name.strip().replace("\\", "/")
    if name.lower().startswith("templates/"):
        name = name[len("templates/") :]
    if name.lower().endswith(".png"):
        name = name[:-4]
    parts = []
    for part in name.split("/"):
        part = BAD_CHARS.sub("_", part).strip("._")
        if part:
            parts.append(part)
    return "/".join(parts) or "template"


class PickerPanel(ctk.CTkFrame):
    """拾取器主体（可嵌进 Toplevel，也可独立作为根窗口）。"""

    def __init__(self, master, root: Optional[str] = None, mode: str = "screen", adb_config: Optional[dict] = None):
        super().__init__(master)
        self.root_dir = os.path.abspath(root or project_root())
        self.mode = "adb" if str(mode).lower() == "adb" else "screen"
        self.adb_config = dict(adb_config or {})

        self._frame_bgr = None  # 原始截图
        self._pil = None
        self._photo = None
        self._scale = 1.0
        self._ox = 0
        self._oy = 0
        self._origin = (0, 0)  # 截图左上角对应的屏幕/设备坐标
        self._point: Optional[Tuple[int, int]] = None  # 图片像素坐标
        self._sel: Optional[Tuple[int, int, int, int]] = None  # 图片像素坐标 x0,y0,x1,y1
        self._drag_start = None
        self._pin: Optional[Tuple[int, int]] = None
        self._source = CaptureSource(self.mode, self.adb_config)
        self._backend = None
        self._adb: Optional[AdbBackend] = None
        self._adb_key = None
        self._screen_key = None
        self._busy = False
        self._auto_job = None
        self.builder = None  # 任务 JSON 构建器（开发者工具）

        self._build_ui()
        self._set_mode(self.mode)
        self.after(200, self.capture)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 6))

        self.mode_seg = ctk.CTkSegmentedButton(
            header,
            values=["屏幕截取", "ADB 投屏"],
            command=self._on_mode_change,
        )
        self.mode_seg.set("ADB 投屏" if self.mode == "adb" else "屏幕截取")
        self.mode_seg.pack(side="left")

        self.target_entry = ctk.CTkEntry(
            header, width=220, placeholder_text="窗口标题（留空=整屏）"
        )
        self.target_entry.pack(side="left", padx=8)
        self.target_entry.insert(0, str(self.adb_config.get("window_title") or ""))

        ctk.CTkButton(header, text="重新定位", width=80, command=self.capture).pack(side="left")

        self.auto_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            header, text="自动刷新", variable=self.auto_var, command=self._toggle_auto, width=90
        ).pack(side="left", padx=10)

        self.status = ctk.CTkLabel(header, text="准备就绪", text_color="gray")
        self.status.pack(side="right")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        self.canvas = ctk.CTkCanvas(
            body, width=CANVAS_W, height=CANVAS_H, bg="#111111", highlightthickness=0
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_move)
        self.canvas.bind("<Configure>", lambda _e: self._render())

        side = ctk.CTkScrollableFrame(body, width=310)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)

        ctk.CTkLabel(side, text="坐标", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self.lbl_abs = ctk.CTkLabel(side, text="绝对：—", anchor="w")
        self.lbl_abs.pack(anchor="w", padx=12, pady=2)
        self.lbl_win = ctk.CTkLabel(side, text="画面内：—", anchor="w")
        self.lbl_win.pack(anchor="w", padx=12, pady=2)
        self.lbl_rel = ctk.CTkLabel(side, text="rel1000：—", anchor="w")
        self.lbl_rel.pack(anchor="w", padx=12, pady=2)
        self.lbl_sel = ctk.CTkLabel(side, text="选区：—", anchor="w")
        self.lbl_sel.pack(anchor="w", padx=12, pady=2)

        ctk.CTkLabel(side, text="操作", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=12, pady=(14, 4)
        )
        ctk.CTkButton(side, text="📋 复制 tap", command=self.copy_tap).pack(fill="x", padx=12, pady=3)
        ctk.CTkButton(side, text="🖼 框选存模板 + 复制 image_tap", command=self.save_template).pack(
            fill="x", padx=12, pady=3
        )

        ctk.CTkLabel(side, text="采集到任务（开发者）", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=12, pady=(14, 4)
        )
        ctk.CTkButton(side, text="➕ 采集当前点 → 点位池", command=self.collect_point).pack(
            fill="x", padx=12, pady=3
        )
        ctk.CTkButton(
            side, text="🖼 框选存模板并采集（image_tap）",
            command=lambda: self.save_template(collect=True),
        ).pack(fill="x", padx=12, pady=3)
        ctk.CTkButton(side, text="🧱 打开任务构建器", command=self.open_builder).pack(
            fill="x", padx=12, pady=3
        )
        ctk.CTkButton(side, text="🎯 在当前点测试点击", command=self.test_click).pack(
            fill="x", padx=12, pady=3
        )
        ctk.CTkButton(side, text="📌 设为起点（swipe）", command=self.set_pin).pack(
            fill="x", padx=12, pady=3
        )
        ctk.CTkButton(side, text="📋 复制 swipe", command=self.copy_swipe).pack(
            fill="x", padx=12, pady=3
        )
        self.overlay_btn = ctk.CTkButton(
            side, text="🖥 全屏悬浮拾取（Esc 退出）", command=self.open_overlay
        )
        self.overlay_btn.pack(fill="x", padx=12, pady=3)
        ctk.CTkButton(side, text="📂 打开模板目录", command=self.open_templates).pack(
            fill="x", padx=12, pady=3
        )

        ctk.CTkLabel(side, text="JSON 片段", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=12, pady=(14, 4)
        )
        self.json_box = ctk.CTkTextbox(side, height=110, font=ctk.CTkFont(family="Menlo", size=11))
        self.json_box.pack(fill="x", padx=12, pady=(0, 10))
        self.json_box.insert("1.0", "在画面上点一下或框选一块区域")
        self.json_box.configure(state="disabled")

        hint = ctk.CTkLabel(
            self,
            text="提示：点选=取点，拖拽=框选；F2 刷新画面，F3 存模板。采集到的点位在「任务构建器」里一键合成 tap_list。",
            text_color="gray",
        )
        hint.pack(anchor="w", padx=14, pady=(6, 10))

        self.bind_all("<F2>", lambda _e: self.capture())
        self.bind_all("<F3>", lambda _e: self.save_template())

    # ------------------------------------------------------------------ 模式
    def _set_mode(self, mode: str):
        self.mode = mode
        self._source.close()
        self._source = CaptureSource(mode, self.adb_config)
        if mode == "adb":
            self.target_entry.configure(placeholder_text="设备地址，如 127.0.0.1:7555")
            self.target_entry.delete(0, "end")
            self.target_entry.insert(0, str(self.adb_config.get("device") or ""))
            self.overlay_btn.configure(state="disabled")
        else:
            self.target_entry.configure(placeholder_text="窗口标题（留空=整屏）")
            self.target_entry.delete(0, "end")
            self.target_entry.insert(0, str(self.adb_config.get("window_title") or ""))
            self.overlay_btn.configure(state="normal")

    def _on_mode_change(self, value: str):
        self._set_mode("adb" if value.startswith("ADB") else "screen")
        self.capture()

    def _set_status(self, text: str, color: str = "gray"):
        self.status.configure(text=text, text_color=color)

    # ------------------------------------------------------------------ 截图
    def _toggle_auto(self):
        if self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None
        if self.auto_var.get():
            self._schedule_auto()

    def _schedule_auto(self):
        if not self.auto_var.get():
            return
        self.capture()
        interval = 1200 if self.mode == "adb" else 2000
        self._auto_job = self.after(interval, self._schedule_auto)

    def capture(self):
        if self._busy:
            return
        self._busy = True
        self._set_status("正在截取画面…", "#f0ad4e")
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _capture_worker(self):
        try:
            if self.mode == "adb":
                frame, origin = self._capture_adb()
            else:
                frame, origin = self._capture_screen()
        except Exception as exc:  # 后台线程里的异常要带回主线程
            err = exc
            self.after(0, lambda: self._on_capture_error(err))
            return
        self.after(0, lambda: self._on_captured(frame, origin))

    def _capture_adb(self):
        """ADB 投屏取一帧（截图逻辑在 tools/capture.py，与任务添加器共用）。"""
        frame, origin = self._source.capture(self.target_entry.get().strip())
        self._adb = self._source._adb  # noqa: SLF001 - 兼容旧的测试点击路径
        return frame, origin

    def _capture_screen(self):
        frame, origin = self._source.capture(self.target_entry.get().strip(), relocate=True)
        self._backend = self._source._backend  # noqa: SLF001
        return frame, origin

    def _on_capture_error(self, exc: Exception):
        self._busy = False
        self._set_status(f"截图失败：{exc}", "#e74c3c")

    def _on_captured(self, frame, origin):
        self._busy = False
        if frame is None or getattr(frame, "size", 0) == 0:
            self._set_status("截图内容为空", "#e74c3c")
            return
        self._frame_bgr = frame
        self._origin = origin
        h, w = frame.shape[:2]
        self._pil = Image.fromarray(frame[:, :, ::-1])
        self._sel = None
        self._set_status(f"画面 {w}x{h}（原点 {origin[0]},{origin[1]}）", "#5cb85c")
        self._render()

    # ------------------------------------------------------------------ 渲染
    def _render(self, full: bool = True):
        c = self.canvas
        if full:
            c.delete("all")
            if self._pil is None:
                return
            cw = max(c.winfo_width(), 200)
            ch = max(c.winfo_height(), 200)
            iw, ih = self._pil.size
            self._scale = min(cw / iw, ch / ih)
            disp_w, disp_h = int(iw * self._scale), int(ih * self._scale)
            self._ox = (cw - disp_w) // 2
            self._oy = (ch - disp_h) // 2
            img = self._pil if (disp_w, disp_h) == (iw, ih) else self._pil.resize(
                (disp_w, disp_h), Image.LANCZOS
            )
            self._photo = ImageTk.PhotoImage(img)
            c.create_image(self._ox, self._oy, anchor="nw", image=self._photo)
            c.create_rectangle(
                self._ox, self._oy, self._ox + disp_w, self._oy + disp_h, outline="#444", width=1
            )
        self._draw_overlay()

    def _draw_overlay(self):
        c = self.canvas
        c.delete("overlay")
        if self._pil is None:
            return
        if self._sel:
            x0, y0, x1, y1 = self._sel
            dx0, dy0 = self._to_disp(x0, y0)
            dx1, dy1 = self._to_disp(x1, y1)
            c.create_rectangle(dx0, dy0, dx1, dy1, outline="#ff3860", width=2, tags="overlay")
            c.create_text(
                dx0 + 4,
                max(dy0 - 10, 8),
                anchor="w",
                text=f"{abs(x1 - x0)}x{abs(y1 - y0)}",
                fill="#ffd166",
                tags="overlay",
            )
        if self._pin:
            px, py = self._to_disp(*self._pin)
            c.create_oval(px - 4, py - 4, px + 4, py + 4, outline="#00e5ff", width=2, tags="overlay")
        if self._point:
            x, y = self._to_disp(*self._point)
            c.create_line(x - 14, y, x + 14, y, fill="#00e5ff", tags="overlay")
            c.create_line(x, y - 14, x, y + 14, fill="#00e5ff", tags="overlay")

    def _to_disp(self, ix: float, iy: float) -> Tuple[float, float]:
        return (self._ox + ix * self._scale, self._oy + iy * self._scale)

    def _to_image(self, dx: float, dy: float) -> Optional[Tuple[int, int]]:
        if self._pil is None or self._scale <= 0:
            return None
        ix = (dx - self._ox) / self._scale
        iy = (dy - self._oy) / self._scale
        w, h = self._pil.size
        if ix < 0 or iy < 0 or ix >= w or iy >= h:
            return None
        return (int(ix), int(iy))

    # ------------------------------------------------------------------ 鼠标
    def _on_press(self, event):
        point = self._to_image(event.x, event.y)
        if point is None:
            return
        self._drag_start = point
        self._sel = None
        self._point = point
        self._update_labels()
        self._draw_overlay()

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        point = self._to_image(event.x, event.y)
        if point is None:
            return
        x0, y0 = self._drag_start
        self._sel = (min(x0, point[0]), min(y0, point[1]), max(x0, point[0]), max(y0, point[1]))
        self._point = point
        self._update_labels()
        self._draw_overlay()

    def _on_release(self, event):
        if self._drag_start is None:
            return
        point = self._to_image(event.x, event.y) or self._drag_start
        x0, y0 = self._drag_start
        if abs(point[0] - x0) < 4 and abs(point[1] - y0) < 4:
            self._sel = None
            self._point = (point[0], point[1])
        else:
            self._sel = (min(x0, point[0]), min(y0, point[1]), max(x0, point[0]), max(y0, point[1]))
            self._point = ((self._sel[0] + self._sel[2]) // 2, (self._sel[1] + self._sel[3]) // 2)
        self._drag_start = None
        self._update_labels()
        self._draw_overlay()

    def _on_move(self, event):
        point = self._to_image(event.x, event.y)
        if point is None or self._drag_start is not None:
            return
        self._point = point
        self._update_labels()
        self._draw_overlay()

    # ------------------------------------------------------------------ 坐标
    def _size(self) -> Tuple[int, int]:
        if self._pil is None:
            return (0, 0)
        return self._pil.size

    def _update_labels(self):
        if self._point is None or self._pil is None:
            return
        ix, iy = self._point
        w, h = self._size()
        ax, ay = ix + self._origin[0], iy + self._origin[1]
        rx, ry = px_to_rel1000(ix, iy, w, h)
        self.lbl_abs.configure(text=f"绝对：({ax}, {ay})")
        self.lbl_win.configure(text=f"画面内：({ix}, {iy})  画面 {w}x{h}")
        self.lbl_rel.configure(text=f"rel1000：({rx}, {ry})")
        if self._sel:
            x0, y0, x1, y1 = self._sel
            self.lbl_sel.configure(
                text=f"选区：{x1 - x0}x{y1 - y0} @ ({x0 + self._origin[0]}, {y0 + self._origin[1]})"
            )
        else:
            self.lbl_sel.configure(text="选区：—")

    # ------------------------------------------------------------------ 输出
    @staticmethod
    def _sanitize(name: str) -> str:
        return _sanitize_name(name)

    def _push_json(self, snippet: str):
        self.json_box.configure(state="normal")
        self.json_box.delete("1.0", "end")
        self.json_box.insert("1.0", snippet)
        self.json_box.configure(state="disabled")
        try:
            self.clipboard_clear()
            self.clipboard_append(snippet)
            self._set_status("JSON 已复制到剪贴板", "#5cb85c")
        except Exception:
            self._set_status("已生成 JSON（复制失败，可手动选中）", "#f0ad4e")

    def copy_tap(self):
        if self._point is None:
            self._set_status("请先在画面上点选位置", "#f0ad4e")
            return
        ix, iy = self._point
        w, h = self._size()
        rx, ry = px_to_rel1000(ix, iy, w, h)
        snippet = (
            '{\n'
            '  "action": "tap",\n'
            f'  "x": {rx},\n'
            f'  "y": {ry},\n'
            '  "coord": "rel1000"\n'
            '}'
        )
        self._push_json(snippet)

    def copy_swipe(self):
        if self._pin is None or self._point is None:
            self._set_status("先「设为起点」，再点选终点", "#f0ad4e")
            return
        w, h = self._size()
        x1, y1 = px_to_rel1000(self._pin[0], self._pin[1], w, h)
        x2, y2 = px_to_rel1000(self._point[0], self._point[1], w, h)
        snippet = (
            '{\n'
            '  "action": "swipe",\n'
            f'  "x1": {x1}, "y1": {y1},\n'
            f'  "x2": {x2}, "y2": {y2},\n'
            '  "coord": "rel1000",\n'
            '  "duration": 400\n'
            '}'
        )
        self._push_json(snippet)

    def set_pin(self):
        if self._point is None:
            self._set_status("请先点选起点", "#f0ad4e")
            return
        self._pin = self._point
        self._draw_overlay()
        self._set_status("起点已记录，再点终点后复制 swipe", "#5cb85c")

    def test_click(self):
        if self._point is None:
            self._set_status("请先点选位置", "#f0ad4e")
            return
        ix, iy = self._point
        ax, ay = ix + self._origin[0], iy + self._origin[1]
        try:
            self._source.tap_abs(ax, ay)
        except Exception as exc:
            self._set_status(f"点击失败：{exc}", "#e74c3c")
            return
        self._set_status(f"已点击绝对坐标 ({ax}, {ay})", "#5cb85c")

    def save_template(self, collect: bool = False):
        if self._pil is None or self._frame_bgr is None:
            self._set_status("请先刷新画面", "#f0ad4e")
            return
        if not self._sel:
            self._set_status("请先框选一块区域（拖拽）", "#f0ad4e")
            return
        x0, y0, x1, y1 = self._sel
        if x1 - x0 < 6 or y1 - y0 < 6:
            self._set_status("选区太小，至少 6x6", "#f0ad4e")
            return

        dialog = ctk.CTkInputDialog(
            text="模板文件名（可带子目录，如 ui/btn_home）：", title="保存模板"
        )
        name = dialog.get_input()
        if not name:
            return
        rel = self._sanitize(name)
        path = os.path.join(self.root_dir, "templates", *rel.split("/")) + ".png"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        crop = self._frame_bgr[y0:y1, x0:x1]
        try:
            ok, buf = vision.cv2.imencode(".png", crop)
            if not ok:
                raise RuntimeError("编码失败")
            buf.tofile(path)
        except Exception as exc:
            self._set_status(f"保存失败：{exc}", "#e74c3c")
            return

        rel_path = f"templates/{rel}.png"
        snippet = (
            '{\n'
            '  "action": "image_tap",\n'
            f'  "template": "{rel_path}",\n'
            '  "threshold": 0.85\n'
            '}'
        )
        self._push_json(snippet)
        if collect:
            builder = self.open_builder()
            if builder is not None:
                builder.add_step(json.loads(snippet))
        self._set_status(f"已保存 {rel_path}（{x1 - x0}x{y1 - y0}）", "#5cb85c")

    # ------------------------------------------------------------------ 采集
    def open_builder(self):
        """打开（或聚焦）任务 JSON 构建器。"""
        try:
            from tools.builder import TaskBuilder
        except Exception as exc:
            self._set_status(f"无法打开任务构建器：{exc}", "#e74c3c")
            return None
        if self.builder is not None and self.builder.winfo_exists():
            self.builder.focus_force()
            return self.builder
        self.builder = TaskBuilder(self, root=self.root_dir)
        self.builder.focus_force()
        return self.builder

    def collect_point(self):
        """把当前点按 rel1000 追加进构建器的点位池（连续采集）。"""
        if self._point is None:
            self._set_status("请先在画面上点选位置", "#f0ad4e")
            return
        builder = self.open_builder()
        if builder is None:
            return
        ix, iy = self._point
        w, h = self._size()
        rx, ry = px_to_rel1000(ix, iy, w, h)
        builder.add_point(rx, ry)
        self._set_status(
            f"已采集 rel1000 ({rx}, {ry}) → 点位池第 {len(builder._points)} 个", "#5cb85c"
        )

    def open_templates(self):
        try:
            open_folder(os.path.join(self.root_dir, "templates"))
        except Exception as exc:
            self._set_status(f"打开目录失败：{exc}", "#e74c3c")

    # ------------------------------------------------------------------ 悬浮
    def open_overlay(self):
        """全屏悬浮拾取：冻结一帧桌面，1:1 点选。仅屏幕模式。"""
        if self.mode == "adb":
            self._set_status("ADB 模式请直接在投屏画面上点选", "#f0ad4e")
            return
        try:
            import mss

            with mss.mss() as sct:
                mon = sct.monitors[1]
                raw = sct.grab(mon)
                frame = vision.np.ascontiguousarray(vision.np.asarray(raw)[:, :, :3])
            origin = (int(mon["left"]), int(mon["top"]))
        except Exception as exc:
            self._set_status(f"抓取全屏失败：{exc}", "#e74c3c")
            return

        OverlayPicker(
            self,
            frame=frame,
            origin=origin,
            on_done=self._on_overlay_done,
        )

    def _on_overlay_done(self, frame, origin, point, sel):
        self._frame_bgr = frame
        self._origin = origin
        self._pil = Image.fromarray(frame[:, :, ::-1])
        self._scale = 1.0
        self._point = point
        self._sel = sel
        self._render()
        self._update_labels()
        self._set_status("已从全屏悬浮拾取（基准=全屏）", "#5cb85c")


class OverlayPicker(ctk.CTkToplevel):
    """全屏 1:1 悬浮层：点选取点，拖拽取选区，Esc 关闭。"""

    def __init__(self, master, frame, origin, on_done):
        super().__init__(master)
        self.frame_bgr = frame
        self.origin = origin
        self.on_done = on_done
        self._start = None
        self._sel = None
        self._point = None

        h, w = frame.shape[:2]
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{w}x{h}+{origin[0]}+{origin[1]}")

        self.canvas = ctk.CTkCanvas(self, width=w, height=h, highlightthickness=0, bg="black")
        self.canvas.pack(fill="both", expand=True)
        self._photo = ImageTk.PhotoImage(Image.fromarray(frame[:, :, ::-1]))
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.focus_force()

    def _press(self, event):
        self._start = (event.x, event.y)
        self._sel = None
        self.canvas.delete("overlay")
        self.canvas.create_line(event.x - 12, event.y, event.x + 12, event.y, fill="#00e5ff", tags="overlay")
        self.canvas.create_line(event.x, event.y - 12, event.x, event.y + 12, fill="#00e5ff", tags="overlay")

    def _drag(self, event):
        if not self._start:
            return
        x0, y0 = self._start
        self.canvas.delete("sel")
        self.canvas.create_rectangle(
            min(x0, event.x), min(y0, event.y), max(x0, event.x), max(y0, event.y),
            outline="#ff3860", width=2, tags="sel",
        )

    def _release(self, event):
        if not self._start:
            return
        x0, y0 = self._start
        if abs(event.x - x0) < 4 and abs(event.y - y0) < 4:
            point = (event.x, event.y)
            sel = None
        else:
            sel = (min(x0, event.x), min(y0, event.y), max(x0, event.x), max(y0, event.y))
            point = ((sel[0] + sel[2]) // 2, (sel[1] + sel[3]) // 2)
        self.destroy()
        self.on_done(self.frame_bgr, self.origin, point, sel)


class PickerWindow(ctk.CTkToplevel):
    def __init__(self, master, root: Optional[str] = None, mode: str = "screen", adb_config: Optional[dict] = None):
        super().__init__(master)
        self.title("坐标拾取器 · 这城有良田")
        self.geometry("1120x760")
        self.panel = PickerPanel(self, root=root, mode=mode, adb_config=adb_config)
        self.panel.pack(fill="both", expand=True)
        self.after(300, self.focus_force)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="坐标拾取器 / 模板截图工具")
    parser.add_argument("--mode", choices=["screen", "adb"], default="screen")
    parser.add_argument("--adb", default="", help="adb 路径")
    parser.add_argument("--device", default="127.0.0.1:7555", help="设备地址")
    parser.add_argument("--window", default="", help="窗口标题")
    args = parser.parse_args(argv)

    if not vision.CV_AVAILABLE:
        print("缺少 opencv-python / numpy，请先 pip install -r requirements.txt")
        return 1

    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    root.title("坐标拾取器 · 这城有良田")
    root.geometry("1120x760")
    panel = PickerPanel(
        root,
        mode=args.mode,
        adb_config={"adb_path": args.adb, "device": args.device, "window_title": args.window},
    )
    panel.pack(fill="both", expand=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
