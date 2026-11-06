"""任务添加器：录任务，而不是抄坐标。

原「坐标拾取器」只负责取点，取完还得手写 JSON；这个窗口把整条链路连起来：

1. 填任务名称（key 自动生成，可改）
2. 选步骤类型：点击 / 点位列表 / 识图点击 / 滑动 / 输入文本 / 等待 / 按键 / 调用子任务
3. 点【🎯 开始拾取】，在游戏画面上点一下（滑动点两下、识图拖拽框选）
4. 坐标自动进步骤，步骤列表按顺序往下排
5. 校验 → 保存到 ``tasks/*.json``

产出规范见 FEATURES.md 第八节：坐标只存 ``rel1000``，模板只存相对路径。

用法::

    python main.py adder                  # 主入口（开发者工具）
    python tools/adder.py                 # 独立窗口
    python main.py picker                 # 兼容别名，打开的是同一个窗口
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from typing import Dict, List, Optional, Tuple

import customtkinter as ctk
from tkinter import messagebox
from PIL import Image, ImageTk

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import task_schema, vision  # noqa: E402
from core.coords import px_to_rel1000  # noqa: E402
from core.logger import Logger  # noqa: E402
from tools import stepkit  # noqa: E402
from tools.capture import CaptureSource, open_folder  # noqa: E402

DEFAULT_FILE = "tasks_dev.json"
CANVAS_W, CANVAS_H = 720, 540

PICK_HINTS = {
    "tap": "拾取中：在画面上点一下（Esc 取消，右键撤销）",
    "swipe": "拾取中：先点起点，再点终点",
    "tap_list": "拾取中：连续点，点完按【✔ 完成本步】",
    "image_tap": "拾取中：拖拽框选要识别的按钮",
    "wait_image": "拾取中：拖拽框选要等待出现的图",
    "wait_gone": "拾取中：拖拽框选要等待消失的图",
    "exists": "拾取中：拖拽框选要判断有无的图",
}


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_float(text, fallback: float) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return fallback


def _as_int(text, fallback: int) -> int:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return fallback


class TaskAdder(ctk.CTkFrame):
    """任务添加器主体（可嵌 Toplevel，也可独立作根窗口内容）。"""

    def __init__(
        self,
        master,
        root: Optional[str] = None,
        mode: str = "screen",
        adb_config: Optional[dict] = None,
        file: str = "",
        key: str = "",
        desc: str = "",
    ):
        super().__init__(master)
        self.root_dir = os.path.abspath(root or project_root())
        self.mode = "adb" if str(mode).lower() == "adb" else "screen"
        self.adb_config = dict(adb_config or {})

        # 画面
        self._source = CaptureSource(self.mode, self.adb_config)
        self._frame_bgr = None
        self._pil = None
        self._photo = None
        self._scale = 1.0
        self._ox = 0
        self._oy = 0
        self._origin: Tuple[int, int] = (0, 0)
        self._point: Optional[Tuple[int, int]] = None
        self._sel: Optional[Tuple[int, int, int, int]] = None
        self._drag_start = None
        self._busy = False
        self._auto_job = None

        # 录入
        self._steps: List[dict] = []
        self._selected: Optional[int] = None
        self._extra_body: Dict[str, object] = {}
        self._pick_action: Optional[str] = None
        self._pick_points: List[Tuple[int, int]] = []
        self._pick_after_capture = False

        # 变量
        self.desc_var = ctk.StringVar(value=desc or "")
        self.key_var = ctk.StringVar(value=key or "")
        self.file_var = ctk.StringVar(value=file or DEFAULT_FILE)
        self.flow_var = ctk.StringVar(value="")
        #: 声明的总步骤数（空 = 不限，步数 = 录了几步就是几步）
        self.steps_total_var = ctk.StringVar(value="")
        self.type_var = ctk.StringVar(value="点击（tap）")
        self.times_var = ctk.StringVar(value="1")
        self.interval_var = ctk.StringVar(value="0.8")
        self.timeout_var = ctk.StringVar(value="10")
        self.duration_var = ctk.StringVar(value="400")
        self.seconds_var = ctk.StringVar(value="1.0")
        self.text_var = ctk.StringVar(value="")
        self.keyevent_var = ctk.StringVar(value="back")
        self.call_var = ctk.StringVar(value="")
        self.count_var = ctk.StringVar(value="3")
        self.continuous_var = ctk.BooleanVar(value=False)
        self.topmost_var = ctk.BooleanVar(value=True)

        self._build_ui()
        self._sync_mode_ui()
        self._render_steps()
        self._render_params()
        self.after(150, self._refresh_choices)
        self.after(250, self.capture)

    # ================================================================== UI
    def _build_ui(self):
        self._build_identity()
        self._build_body()
        self._build_footer()

        # 总步数一改就刷新「已录 N / M 步」，不用等下一次录入
        self.steps_total_var.trace_add("write", lambda *_: self._sync_step_total())
        self._sync_step_total()

        top = self.winfo_toplevel()
        top.bind("<F2>", lambda _e: self.capture(), add="+")
        top.bind("<Escape>", lambda _e: self.cancel_pick(), add="+")

    def _build_identity(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 2))

        ctk.CTkLabel(head, text="任务名称").pack(side="left")
        ctk.CTkEntry(
            head, textvariable=self.desc_var, width=190, placeholder_text="领取每日奖励"
        ).pack(side="left", padx=(6, 2))
        ctk.CTkButton(head, text="✨", width=32, command=self.sync_key).pack(side="left")

        ctk.CTkLabel(head, text="key").pack(side="left", padx=(10, 0))
        ctk.CTkEntry(
            head, textvariable=self.key_var, width=150, placeholder_text="claimRewards"
        ).pack(side="left", padx=(6, 12))

        ctk.CTkLabel(head, text="写入文件").pack(side="left")
        self.file_box = ctk.CTkComboBox(
            head, variable=self.file_var, values=[DEFAULT_FILE], width=160
        )
        self.file_box.pack(side="left", padx=(6, 12))

        ctk.CTkLabel(head, text="加入流程").pack(side="left")
        self.flow_box = ctk.CTkComboBox(head, variable=self.flow_var, values=[""], width=110)
        self.flow_box.pack(side="left", padx=(6, 6))

        ctk.CTkButton(head, text="🆕 新建", width=76, command=self.new_task).pack(side="right", padx=4)
        ctk.CTkButton(head, text="📥 载入", width=76, command=self.load_existing).pack(
            side="right", padx=4
        )

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(0, 2))

        self.mode_seg = ctk.CTkSegmentedButton(
            bar, values=["屏幕截取", "ADB 投屏"], command=self._on_mode_change
        )
        self.mode_seg.pack(side="left")

        self.target_entry = ctk.CTkEntry(bar, width=210)
        self.target_entry.pack(side="left", padx=8)

        ctk.CTkButton(bar, text="🔄 刷新画面", width=96, command=self.capture).pack(side="left")
        ctk.CTkButton(bar, text="👆 测试点击", width=96, command=self.test_click).pack(
            side="left", padx=6
        )

        self.topmost_switch = ctk.CTkSwitch(
            bar, text="窗口置顶", variable=self.topmost_var, command=self._apply_topmost
        )
        self.topmost_switch.pack(side="left", padx=8)

        self.status = ctk.CTkLabel(bar, text="准备就绪", text_color="gray")
        self.status.pack(side="right")

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(4, 0))

        left = ctk.CTkFrame(body, width=470)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        title_row = ctk.CTkFrame(left, fg_color="transparent")
        title_row.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(
            title_row, text="步骤（顺序 = 执行顺序）", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(side="left")
        for symbol, command in (
            ("✕", self.delete_step),
            ("⧉", self.duplicate_step),
            ("↑", lambda: self.move_step(-1)),
            ("↓", lambda: self.move_step(1)),
        ):
            ctk.CTkButton(
                title_row,
                text=symbol,
                width=28,
                height=24,
                fg_color="transparent",
                border_width=1,
                text_color=("black", "white"),
                command=command,
            ).pack(side="right", padx=2)

        meta_row = ctk.CTkFrame(left, fg_color="transparent")
        meta_row.pack(fill="x", padx=10, pady=(0, 2))
        ctk.CTkLabel(meta_row, text="总步数").pack(side="left")
        self.steps_total_entry = ctk.CTkEntry(
            meta_row, textvariable=self.steps_total_var, width=54, placeholder_text="不限"
        )
        self.steps_total_entry.pack(side="left", padx=(6, 10))
        self.steps_progress = ctk.CTkLabel(meta_row, text="", text_color="gray")
        self.steps_progress.pack(side="left")

        self.steps_frame = ctk.CTkScrollableFrame(left, height=300)
        self.steps_frame.pack(fill="both", expand=True, padx=8, pady=(2, 6))
        add = ctk.CTkFrame(left)
        add.pack(fill="x", padx=8, pady=(0, 8))

        row1 = ctk.CTkFrame(add, fg_color="transparent")
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(row1, text="类型").pack(side="left")
        self.type_menu = ctk.CTkOptionMenu(
            row1,
            variable=self.type_var,
            values=list(stepkit.TYPE_ACTIONS),
            width=176,
            command=self._on_type_change,
        )
        self.type_menu.pack(side="left", padx=(6, 10))

        ctk.CTkLabel(row1, text="次数").pack(side="left")
        self.times_entry = ctk.CTkEntry(row1, textvariable=self.times_var, width=50)
        self.times_entry.pack(side="left", padx=(4, 10))
        ctk.CTkLabel(row1, text="间隔").pack(side="left")
        self.interval_entry = ctk.CTkEntry(row1, textvariable=self.interval_var, width=54)
        self.interval_entry.pack(side="left", padx=(4, 0))

        self.params_frame = ctk.CTkFrame(add, fg_color="transparent")
        self.params_frame.pack(fill="x", padx=8, pady=(0, 2))

        actions = ctk.CTkFrame(add, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(2, 4))
        ctk.CTkButton(
            actions, text="🎯 开始拾取", width=120, command=self.start_pick
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="✔ 完成本步",
            width=100,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
            command=self.finish_step,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="✕ 取消拾取",
            width=96,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
            command=self.cancel_pick,
        ).pack(side="left", padx=6)
        ctk.CTkCheckBox(
            actions, text="连续录入", variable=self.continuous_var, width=90
        ).pack(side="left", padx=8)

        self.pick_status = ctk.CTkLabel(add, text="选好类型后点【🎯 开始拾取】", text_color="gray")
        self.pick_status.pack(anchor="w", padx=10, pady=(0, 8))

        # 右：画面
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="right", fill="both", expand=True, padx=(10, 0))

        self.canvas = ctk.CTkCanvas(
            right, width=CANVAS_W, height=CANVAS_H, bg="#111111", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_move)
        self.canvas.bind("<Button-3>", lambda _e: self.undo_point())
        self.canvas.bind("<Button-2>", lambda _e: self.undo_point())  # macOS 右键
        self.canvas.bind("<Control-Button-1>", lambda _e: self.undo_point())
        self.canvas.bind("<Configure>", lambda _e: self._render())

        self.coord_label = ctk.CTkLabel(
            right, text="画面内 —　rel1000 —　绝对 —", text_color="gray", anchor="w"
        )
        self.coord_label.pack(fill="x", pady=(4, 0))

    def _build_footer(self):
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=12, pady=8)

        self.footer_status = ctk.CTkLabel(
            foot, text="录完记得校验再保存。", text_color="gray", wraplength=420, justify="left"
        )
        self.footer_status.pack(side="left")

        ctk.CTkButton(foot, text="💾 保存到 tasks/", width=150, command=self.save).pack(side="right")
        ctk.CTkButton(foot, text="▶ 试运行（演练）", width=140, command=self.dry_run).pack(
            side="right", padx=6
        )
        ctk.CTkButton(foot, text="📋 复制 JSON", width=120, command=self.copy_json).pack(
            side="right", padx=6
        )
        ctk.CTkButton(foot, text="✔ 校验", width=90, command=self.validate).pack(side="right", padx=6)
        ctk.CTkButton(
            foot,
            text="📂 模板目录",
            width=110,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
            command=lambda: open_folder(os.path.join(self.root_dir, "templates")),
        ).pack(side="left", padx=8)

    # ================================================================== 模式
    def _sync_mode_ui(self):
        if self.mode == "adb":
            self.mode_seg.set("ADB 投屏")
            self.target_entry.configure(placeholder_text="设备地址，如 127.0.0.1:7555")
            value = str(self.adb_config.get("device") or "127.0.0.1:7555")
        else:
            self.mode_seg.set("屏幕截取")
            self.target_entry.configure(placeholder_text="窗口标题（留空=整屏）")
            value = str(self.adb_config.get("window_title") or "")
        self.target_entry.delete(0, "end")
        self.target_entry.insert(0, value)
        self._apply_topmost()

    def _on_mode_change(self, value: str):
        self.mode = "adb" if value.startswith("ADB") else "screen"
        self._source.close()
        self._source = CaptureSource(self.mode, self.adb_config)
        self._sync_mode_ui()
        self.capture()

    def _apply_topmost(self):
        try:
            self.winfo_toplevel().attributes("-topmost", bool(self.topmost_var.get()))
        except Exception:
            pass

    # ================================================================== 截图
    def capture(self):
        if self._busy:
            return
        self._busy = True
        # Tk 控件只能在主线程读，先把目标取出来再进后台线程
        target = self.target_entry.get().strip()
        self._set_status("正在截取画面…", "#f0ad4e")
        threading.Thread(target=self._capture_worker, args=(target,), daemon=True).start()

    def _capture_worker(self, target: str):
        try:
            frame, origin = self._source.capture(target, relocate=True)
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_capture_error(e))
            return
        self.after(0, lambda: self._on_captured(frame, origin))

    def _on_capture_error(self, exc: Exception):
        self._busy = False
        self._pick_after_capture = False
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
        self._set_status(f"画面 {w}x{h}（{self._source.last_label or self.mode}）", "#5cb85c")
        self._render()
        if self._pick_after_capture:
            self._pick_after_capture = False
            self.start_pick()

    # ================================================================== 渲染
    def _render(self, full: bool = True):
        canvas = self.canvas
        if full:
            canvas.delete("all")
            if self._pil is None:
                return
            cw = max(canvas.winfo_width(), 200)
            ch = max(canvas.winfo_height(), 200)
            iw, ih = self._pil.size
            self._scale = min(cw / iw, ch / ih)
            disp_w, disp_h = int(iw * self._scale), int(ih * self._scale)
            self._ox = (cw - disp_w) // 2
            self._oy = (ch - disp_h) // 2
            img = self._pil if (disp_w, disp_h) == (iw, ih) else self._pil.resize(
                (disp_w, disp_h), Image.LANCZOS
            )
            self._photo = ImageTk.PhotoImage(img)
            canvas.create_image(self._ox, self._oy, anchor="nw", image=self._photo)
            canvas.create_rectangle(
                self._ox, self._oy, self._ox + disp_w, self._oy + disp_h, outline="#444"
            )
        self._draw_overlay()

    def _draw_overlay(self):
        canvas = self.canvas
        canvas.delete("overlay")
        if self._pil is None:
            return

        if self._sel:
            x0, y0, x1, y1 = self._sel
            dx0, dy0 = self._to_disp(x0, y0)
            dx1, dy1 = self._to_disp(x1, y1)
            canvas.create_rectangle(
                dx0, dy0, dx1, dy1, outline="#ff3860", width=2, tags="overlay"
            )
            canvas.create_text(
                dx0 + 4,
                max(dy0 - 10, 10),
                anchor="w",
                text=f"{abs(x1 - x0)}x{abs(y1 - y0)}",
                fill="#ffd166",
                tags="overlay",
            )

        # 当前拾取会话里已经点下的点（点位列表 / 滑动起点）
        for index, point in enumerate(self._pick_points):
            px, py = self._to_disp(*point)
            canvas.create_oval(
                px - 7, py - 7, px + 7, py + 7, outline="#00e5ff", width=2, tags="overlay"
            )
            canvas.create_text(
                px, py - 12, text=str(index + 1), fill="#00e5ff", tags="overlay"
            )

        if self._point:
            x, y = self._to_disp(*self._point)
            canvas.create_line(x - 14, y, x + 14, y, fill="#00e5ff", tags="overlay")
            canvas.create_line(x, y - 14, x, y + 14, fill="#00e5ff", tags="overlay")

        self._draw_selected_step()

    def _draw_selected_step(self):
        """把当前选中步骤的坐标画在画面上，方便确认录得对不对。"""
        if self._selected is None or not (0 <= self._selected < len(self._steps)):
            return
        step = self._steps[self._selected]
        canvas = self.canvas
        w, h = self._pil.size
        action = step.get("action")

        def to_disp(rel_x, rel_y):
            return self._to_disp(float(rel_x) * w / 1000.0, float(rel_y) * h / 1000.0)

        if action == "tap" and "x" in step:
            x, y = to_disp(step["x"], step["y"])
            canvas.create_oval(x - 9, y - 9, x + 9, y + 9, outline="#ffd166", width=2, tags="overlay")
        elif action == "swipe" and "x1" in step:
            x1, y1 = to_disp(step["x1"], step["y1"])
            x2, y2 = to_disp(step["x2"], step["y2"])
            canvas.create_line(x1, y1, x2, y2, fill="#ffd166", width=2, arrow="last", tags="overlay")
        elif action == "tap_list":
            for item in step.get("items") or []:
                if "x" not in item:
                    continue
                x, y = to_disp(item["x"], item["y"])
                canvas.create_oval(
                    x - 7, y - 7, x + 7, y + 7, outline="#ffd166", width=2, tags="overlay"
                )

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

    # ================================================================== 鼠标
    def _on_press(self, event):
        point = self._to_image(event.x, event.y)
        if point is None:
            return
        self._drag_start = point
        self._sel = None
        self._point = point
        self._update_coords()
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
        self._update_coords()
        self._draw_overlay()

    def _on_release(self, event):
        if self._drag_start is None:
            return
        start = self._drag_start
        point = self._to_image(event.x, event.y) or start
        dragged = abs(point[0] - start[0]) >= 4 or abs(point[1] - start[1]) >= 4
        if dragged:
            self._sel = (
                min(start[0], point[0]),
                min(start[1], point[1]),
                max(start[0], point[0]),
                max(start[1], point[1]),
            )
            self._point = ((self._sel[0] + self._sel[2]) // 2, (self._sel[1] + self._sel[3]) // 2)
        else:
            self._sel = None
            self._point = point
        self._drag_start = None
        self._update_coords()
        self._draw_overlay()
        if self._pick_action:
            self._handle_pick(self._sel if dragged else None)

    def _on_move(self, event):
        if self._drag_start is not None:
            return
        point = self._to_image(event.x, event.y)
        if point is None:
            return
        self._point = point
        self._update_coords()
        self._draw_overlay()

    def _update_coords(self):
        if self._point is None or self._pil is None:
            return
        ix, iy = self._point
        w, h = self._pil.size
        ax, ay = ix + self._origin[0], iy + self._origin[1]
        rx, ry = px_to_rel1000(ix, iy, w, h)
        self.coord_label.configure(
            text=f"画面内 ({ix}, {iy})　rel1000 ({rx}, {ry})　绝对 ({ax}, {ay})"
        )

    # ================================================================== 拾取
    def start_pick(self):
        action = stepkit.action_of(self.type_var.get())

        if action in stepkit.MANUAL_ACTIONS:
            self._commit_manual(action)
            return

        if self._pil is None:
            self._pick_after_capture = True
            self._set_pick_status("正在截取画面，截好后自动进入拾取…", "#f0ad4e")
            self.capture()
            return

        self._pick_action = action
        self._pick_points.clear()
        self._sel = None
        self.canvas.configure(cursor="crosshair")
        self._set_pick_status(PICK_HINTS.get(action, "拾取中：在画面上点一下"), "#2d8cf0")
        self._draw_overlay()

    def _handle_pick(self, sel):
        action = self._pick_action
        if action is None:
            return

        if stepkit.is_image_action(action):
            if not sel:
                self._set_pick_status("识图类步骤需要拖拽框选一块区域", "#f0ad4e")
                return
            self._save_template(action, sel)
            return

        point = self._point
        if point is None:
            return

        if stepkit.is_two_point_action(action):
            self._pick_points.append(point)
            if len(self._pick_points) < 2:
                self._set_pick_status("起点已记录，再点终点", "#2d8cf0")
                self._draw_overlay()
                return
            (x1, y1), (x2, y2) = self._pick_points[-2], self._pick_points[-1]
            w, h = self._pil.size
            rx1, ry1 = px_to_rel1000(x1, y1, w, h)
            rx2, ry2 = px_to_rel1000(x2, y2, w, h)
            step = stepkit.make_swipe(
                rx1, ry1, rx2, ry2, duration=_as_int(self.duration_var.get(), 400)
            )
            self._commit(step)
            return

        if stepkit.is_list_action(action):
            self._pick_points.append(point)
            self._draw_overlay()
            self._set_pick_status(
                f"已采集 {len(self._pick_points)} 个点位，继续点；点完按【✔ 完成本步】", "#2d8cf0"
            )
            return

        # 单点：点击
        w, h = self._pil.size
        rx, ry = px_to_rel1000(point[0], point[1], w, h)
        step = stepkit.make_tap(
            rx,
            ry,
            times=_as_int(self.times_var.get(), 1),
            interval=_as_float(self.interval_var.get(), 0.8),
        )
        self._commit(step)

    def finish_step(self):
        action = self._pick_action
        if stepkit.is_list_action(action or ""):
            if not self._pick_points:
                self._set_pick_status("还没有采集任何点位", "#f0ad4e")
                return
            w, h = self._pil.size
            points = [px_to_rel1000(x, y, w, h) for x, y in self._pick_points]
            step = stepkit.make_tap_list(
                points,
                step_id=stepkit.unique_step_id(
                    f"{self.key_var.get().strip() or 'task'}_rows", self._steps
                ),
                interval=_as_float(self.interval_var.get(), 0.8),
            )
            self._commit(step)
            return
        if action:
            self._set_pick_status("本类型不需要收尾，按类型重新拾取即可", "#f0ad4e")
            return
        self._set_pick_status("还没进入拾取态：先选类型，再点【🎯 开始拾取】", "#f0ad4e")

    def undo_point(self):
        if self._pick_action and self._pick_points:
            self._pick_points.pop()
            self._draw_overlay()
            self._set_pick_status(f"已撤销一个点，剩 {len(self._pick_points)} 个", "#f0ad4e")
            return
        if self._steps:
            self._selected = len(self._steps) - 1
            self._render_steps()
            self._draw_overlay()
            self._set_pick_status("已选中最后一步，可用「✕」删除", "gray")

    def cancel_pick(self):
        if not self._pick_action and not self._pick_points:
            return
        self._finish_pick("已取消拾取")

    def _finish_pick(self, message: str = ""):
        self._pick_action = None
        self._pick_points.clear()
        self._sel = None
        try:
            self.canvas.configure(cursor="")
        except Exception:
            pass
        if message:
            self._set_pick_status(message, "gray")
        self._draw_overlay()

    def _commit(self, step: dict):
        self._steps.append(step)
        self._selected = len(self._steps) - 1
        self._render_steps()
        self._draw_overlay()
        label, detail = stepkit.describe_step(step)
        count = len(self._steps)
        total = self._declared_total()
        if total and total > 0 and count == total:
            # 声明了总步数又刚好录满：直接告诉他可以收工，省得再数一遍
            self._set_footer(f"已录满 {total} 步，最后一步：{label} {detail}（可以校验保存了）", "#5cb85c")
        elif total and total > 0 and count > total:
            self._set_footer(
                f"已超出总步数 {total} 步：删掉多余的步骤，或把总步数改大", "#e74c3c"
            )
        else:
            self._set_footer(f"已加入第 {count} 步：{label} {detail}", "#5cb85c")
        if not self.continuous_var.get():
            self._finish_pick(f"已加入第 {count} 步，可继续选类型")
        else:
            self._pick_points.clear()
            self._draw_overlay()
            self._set_pick_status(
                PICK_HINTS.get(self._pick_action or "", "连续录入中，继续点"), "#2d8cf0"
            )

    def _commit_manual(self, action: str):
        try:
            if action == "sleep":
                step = stepkit.make_sleep(_as_float(self.seconds_var.get(), 1.0))
            elif action == "text":
                value = self.text_var.get()
                if not value.strip():
                    raise ValueError("请先填写要输入的文本")
                step = stepkit.make_text(value)
            elif action == "keyevent":
                step = stepkit.make_key(self.keyevent_var.get() or "back")
            elif action == "call":
                task = self.call_var.get().strip()
                if not task:
                    raise ValueError("请先选择要调用的子任务")
                step = stepkit.make_call(task)
            elif action == "loop":
                step = stepkit.make_loop(_as_int(self.count_var.get(), 3))
            else:
                raise ValueError(f"{action} 需要拾取，不能手填")
        except ValueError as exc:
            self._set_pick_status(str(exc), "#f0ad4e")
            return
        self._commit(step)
        self._finish_pick("")

    def _save_template(self, action: str, sel):
        if self._frame_bgr is None:
            self._set_pick_status("还没有画面，先刷新", "#f0ad4e")
            return
        x0, y0, x1, y1 = sel
        if x1 - x0 < 6 or y1 - y0 < 6:
            self._set_pick_status("选区太小，至少 6x6", "#f0ad4e")
            return
        if not vision.CV_AVAILABLE:
            self._set_pick_status("缺少 opencv-python / numpy，无法保存模板", "#e74c3c")
            return

        rel = stepkit.template_rel_path(self.key_var.get().strip() or "task", len(self._steps) + 1, action)
        path = os.path.join(self.root_dir, *rel.split("/"))
        # 同名模板已存在就顺延编号，别把上一张覆盖掉
        bump = 1
        while os.path.isfile(path):
            bump += 1
            rel = stepkit.template_rel_path(
                self.key_var.get().strip() or "task", len(self._steps) + bump, action
            )
            path = os.path.join(self.root_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        crop = self._frame_bgr[y0:y1, x0:x1]
        try:
            ok, buf = vision.cv2.imencode(".png", crop)
            if not ok:
                raise RuntimeError("PNG 编码失败")
            buf.tofile(path)
        except Exception as exc:
            self._set_pick_status(f"保存模板失败：{exc}", "#e74c3c")
            return

        step = stepkit.make_image_step(
            action,
            rel,
            threshold=0.85,
            timeout=_as_float(self.timeout_var.get(), 10),
        )
        self._commit(step)
        self._set_footer(f"模板已保存 {rel}（{x1 - x0}x{y1 - y0}）", "#5cb85c")

    def test_click(self):
        if self._point is None:
            self._set_pick_status("先在画面上点一下，再点【测试点击】", "#f0ad4e")
            return
        ix, iy = self._point
        ax, ay = ix + self._origin[0], iy + self._origin[1]
        try:
            self._source.tap_abs(ax, ay)
        except Exception as exc:
            self._set_pick_status(f"测试点击失败：{exc}", "#e74c3c")
            return
        self._set_pick_status(f"已点击绝对坐标 ({ax}, {ay})", "#5cb85c")

    # ================================================================== 步骤列表
    def _render_steps(self):
        self._sync_step_total()
        for widget in self.steps_frame.winfo_children():
            widget.destroy()

        if not self._steps:
            ctk.CTkLabel(
                self.steps_frame,
                text="还没有步骤。\n选好类型 → 点【🎯 开始拾取】→ 在右边画面上点一下。",
                text_color="gray",
                justify="left",
            ).pack(anchor="w", padx=4, pady=8)
            return

        for index, step in enumerate(self._steps):
            row = ctk.CTkFrame(
                self.steps_frame,
                fg_color=("#dbe9f7", "#274156") if index == self._selected else "transparent",
                corner_radius=6,
            )
            row.pack(fill="x", pady=1)

            label, detail = stepkit.describe_step(step)
            text = f"{index + 1}. {label}　{detail}".strip()
            # 用按钮而不是标签：CTkLabel 被点时不触发父级绑定，按钮的 command 稳定
            body = ctk.CTkButton(
                row,
                text=text,
                anchor="w",
                height=26,
                fg_color="transparent",
                hover_color=("#c9d9e8", "#34536b"),
                text_color=("black", "white"),
                command=lambda i=index: self._select_at(i),
            )
            body.pack(side="left", fill="x", expand=True, padx=(2, 0))
            body.bind("<Double-Button-1>", lambda _e, i=index: self._edit_at(i))

            for symbol, command in (
                ("✕", lambda: self._delete_at(index)),
                ("⧉", lambda: self._duplicate_at(index)),
                ("✎", lambda: self._edit_at(index)),
            ):
                ctk.CTkButton(
                    row,
                    text=symbol,
                    width=24,
                    height=22,
                    fg_color="transparent",
                    text_color=("gray30", "gray80"),
                    hover_color=("#c9d9e8", "#34536b"),
                    command=command,
                ).pack(side="right", padx=1)

    def _select_at(self, index: int):
        self._selected = index
        self._render_steps()
        self._draw_overlay()

    def _delete_at(self, index: int):
        if 0 <= index < len(self._steps):
            self._steps.pop(index)
            if self._selected is not None and self._selected >= len(self._steps):
                self._selected = len(self._steps) - 1 if self._steps else None
            self._render_steps()
            self._draw_overlay()
            self._set_footer(f"已删除第 {index + 1} 步（剩 {len(self._steps)} 步）")

    def _duplicate_at(self, index: int):
        if not (0 <= index < len(self._steps)):
            return
        copy = json.loads(json.dumps(self._steps[index]))
        self._steps.insert(index + 1, copy)
        self._selected = index + 1
        self._render_steps()
        self._draw_overlay()
        self._set_footer("已复制该步骤")

    def _edit_at(self, index: int):
        if not (0 <= index < len(self._steps)):
            return
        StepEditor(self, self._steps[index], lambda step: self._apply_edit(index, step))

    def _apply_edit(self, index: int, step: dict):
        if 0 <= index < len(self._steps):
            self._steps[index] = step
            self._selected = index
            self._render_steps()
            self._draw_overlay()
            self._set_footer(f"第 {index + 1} 步已更新", "#5cb85c")

    def delete_step(self):
        if self._selected is None:
            self._set_footer("先在列表里点一下要删的步骤", "#f0ad4e")
            return
        self._delete_at(self._selected)

    def duplicate_step(self):
        if self._selected is None:
            self._set_footer("先在列表里点一下要复制的步骤", "#f0ad4e")
            return
        self._duplicate_at(self._selected)

    def move_step(self, delta: int):
        if self._selected is None:
            self._set_footer("先在列表里点一下要移动的步骤", "#f0ad4e")
            return
        target = self._selected + delta
        if not (0 <= target < len(self._steps)):
            return
        step = self._steps.pop(self._selected)
        self._steps.insert(target, step)
        self._selected = target
        self._render_steps()
        self._draw_overlay()

    # ================================================================== 参数区
    def _on_type_change(self, _value=None):
        self._render_params()
        action = stepkit.action_of(self.type_var.get())
        hints = {
            "tap": "点击：点一下即可；次数>1 就是连点",
            "tap_list": "点位列表：可连点多个位置，最后按【✔ 完成本步】",
            "swipe": "滑动：点起点，再点终点",
            "image_tap": "识图点击：拖拽框选按钮，自动存模板",
            "sleep": "等待：填秒数，不拾取",
            "text": "输入文本：填内容，支持 ${变量}",
            "keyevent": "按键：下拉选，不拾取",
            "call": "调用子任务：下拉选已有任务",
            "loop": "循环：填次数，循环体先留空，事后再改 JSON",
        }
        self._set_pick_status(hints.get(action, "选好后点【🎯 开始拾取】"), "gray")

    def _render_params(self):
        for widget in self.params_frame.winfo_children():
            widget.destroy()

        action = stepkit.action_of(self.type_var.get())
        self.times_entry.configure(state="normal" if action in ("tap", "image_tap") else "disabled")
        self.interval_entry.configure(
            state="normal" if action in ("tap", "image_tap", "swipe", "tap_list") else "disabled"
        )

        if action == "swipe":
            ctk.CTkLabel(self.params_frame, text="时长(ms)").pack(side="left")
            ctk.CTkEntry(self.params_frame, textvariable=self.duration_var, width=64).pack(
                side="left", padx=(4, 10)
            )
        elif action in stepkit.IMAGE_ACTIONS:
            ctk.CTkLabel(self.params_frame, text="超时(s)").pack(side="left")
            ctk.CTkEntry(self.params_frame, textvariable=self.timeout_var, width=56).pack(
                side="left", padx=(4, 10)
            )
        elif action == "sleep":
            ctk.CTkLabel(self.params_frame, text="秒数").pack(side="left")
            ctk.CTkEntry(self.params_frame, textvariable=self.seconds_var, width=60).pack(
                side="left", padx=(4, 10)
            )
        elif action == "text":
            ctk.CTkLabel(self.params_frame, text="内容").pack(side="left")
            ctk.CTkEntry(
                self.params_frame, textvariable=self.text_var, width=240, placeholder_text="支持 ${redeem_code}"
            ).pack(side="left", padx=(4, 10))
        elif action == "keyevent":
            ctk.CTkLabel(self.params_frame, text="按键").pack(side="left")
            ctk.CTkOptionMenu(
                self.params_frame,
                variable=self.keyevent_var,
                values=["back", "home", "enter", "menu", "esc", "tab", "space", "delete"],
                width=110,
            ).pack(side="left", padx=(4, 10))
        elif action == "call":
            ctk.CTkLabel(self.params_frame, text="子任务").pack(side="left")
            self.call_menu = ctk.CTkOptionMenu(
                self.params_frame,
                variable=self.call_var,
                values=self._task_keys() or [""],
                width=180,
            )
            self.call_menu.pack(side="left", padx=(4, 10))
        elif action == "loop":
            ctk.CTkLabel(self.params_frame, text="循环次数").pack(side="left")
            ctk.CTkEntry(self.params_frame, textvariable=self.count_var, width=60).pack(
                side="left", padx=(4, 10)
            )
        else:
            ctk.CTkLabel(self.params_frame, text="", text_color="gray").pack(side="left")

    # ================================================================== 任务信息
    def _declared_total(self) -> Optional[int]:
        """总步数输入框 → 正整数；留空返回 None（不限）；非法值返回 -1（交给校验报错）。"""
        raw = self.steps_total_var.get().strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return -1
        return value if value >= 1 else -1

    def _sync_step_total(self):
        """把「已录 N / 总步数」显示在步骤列表上方，录多了立刻标红。"""
        if not hasattr(self, "steps_progress"):
            return
        total = self._declared_total()
        count = len(self._steps)
        if total == -1:
            self.steps_progress.configure(
                text="总步数得是正整数（留空 = 不限）", text_color="#e74c3c"
            )
            return
        if total is None:
            self.steps_progress.configure(text=f"已录 {count} 步（未限总步数）", text_color="gray")
            return
        color = "#5cb85c" if count == total else ("#e74c3c" if count > total else "#f0ad4e")
        self.steps_progress.configure(text=f"已录 {count} / {total} 步", text_color=color)

    def sync_key(self):
        desc = self.desc_var.get().strip()
        if not desc:
            self._set_footer("先填任务名称，再自动生成 key", "#f0ad4e")
            return
        self.key_var.set(stepkit.auto_key(desc, self._all_keys()))
        self._set_footer(f"key 已生成：{self.key_var.get()}", "#5cb85c")

    def _task_files(self) -> List[str]:
        return task_schema.task_files(os.path.join(self.root_dir, "tasks"))

    def _task_documents(self):
        for path in self._task_files():
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    doc = json.load(fp) or {}
            except (OSError, json.JSONDecodeError):
                continue
            yield path, doc

    def _all_keys(self) -> List[str]:
        keys: List[str] = []
        for _path, doc in self._task_documents():
            for key, body in (doc.get("tasks") or {}).items():
                if isinstance(body, dict):
                    keys.append(key)
        return keys

    def _task_keys(self) -> List[str]:
        return self._all_keys()

    def _flow_names(self) -> List[str]:
        names: List[str] = []
        for _path, doc in self._task_documents():
            for name in (doc.get("flows") or {}):
                if name not in names:
                    names.append(name)
        return names

    def _refresh_choices(self):
        files = [os.path.basename(p) for p in self._task_files()]
        if files:
            self.file_box.configure(values=files)
        flows = self._flow_names()
        self.flow_box.configure(values=flows or [""])
        self._render_params()

    def new_task(self):
        if self._steps and not messagebox.askyesno(
            "新建任务", "当前步骤还没保存，确定清空重来？", parent=self
        ):
            return
        self._steps.clear()
        self._selected = None
        self._extra_body = {}
        self.desc_var.set("")
        self.key_var.set("")
        self.flow_var.set("")
        self.steps_total_var.set("")
        self._render_steps()
        self._draw_overlay()
        self._set_footer("已清空，填任务名称开始录", "gray")

    def load_existing(self):
        options: List[str] = []
        for path, doc in self._task_documents():
            for key, body in (doc.get("tasks") or {}).items():
                if isinstance(body, dict):
                    desc = body.get("desc") or body.get("name") or key
                    options.append(f"{os.path.basename(path)}::{key}（{desc}）")
        if not options:
            self._set_footer("tasks/ 下还没有任务文件", "#f0ad4e")
            return

        dialog = ChoiceDialog(self, "载入已有任务", options)
        self.wait_window(dialog)
        choice = dialog.result
        if not choice:
            return
        filename, rest = choice.split("::", 1)
        key = rest.split("（", 1)[0]
        path = os.path.join(self.root_dir, "tasks", filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                doc = json.load(fp) or {}
        except (OSError, json.JSONDecodeError) as exc:
            self._set_footer(f"读取失败：{exc}", "#e74c3c")
            return

        body = dict((doc.get("tasks") or {}).get(key) or {})
        self.key_var.set(key)
        self.desc_var.set(str(body.get("desc") or body.get("name") or key))
        self.file_var.set(filename)
        self._extra_body = {
            k: v
            for k, v in body.items()
            if k not in ("desc", "name", "steps", "step_count")
        }
        declared = body.get("step_count")
        if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared >= 1:
            self.steps_total_var.set(str(int(declared)))
        else:
            self.steps_total_var.set("")
        self._steps = [dict(s) for s in (body.get("steps") or [])]
        self._selected = None
        self._render_steps()
        self._draw_overlay()
        total = self._declared_total()
        suffix = f"，声明 {total} 步" if total and total > 0 else ""
        self._set_footer(f"已载入 {filename} 的 {key}（{len(self._steps)} 步{suffix}）", "#5cb85c")

    # ================================================================== 校验 / 保存
    def _body(self) -> dict:
        key = self.key_var.get().strip()
        body = dict(self._extra_body)
        # step_count 由输入框唯一决定：留空就删掉，避免载入旧任务后残留旧数字
        body.pop("step_count", None)
        total = self._declared_total()
        if total and total > 0:
            body["step_count"] = total
        body["desc"] = self.desc_var.get().strip() or key
        body["steps"] = self._steps
        return body

    def _checked_body(self) -> Tuple[dict, "task_schema.TaskCheck"]:
        """校验一次，并把补全默认值后的步骤（coord / interval 等）带回。

        校验用目标文件里的 ``defaults``，避免把当前文件的默认值带偏
        （比如 daily.json 把 interval 设成 0.8，就别存成 1.0）。
        """
        body = self._body()
        defaults = None
        path = self._resolve_target()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    raw = json.load(fp) or {}
                if isinstance(raw.get("defaults"), dict):
                    defaults = raw["defaults"]
            except (OSError, json.JSONDecodeError):
                defaults = None
        check = task_schema.validate_task(
            self.key_var.get().strip(), body, self.root_dir, defaults=defaults
        )
        if check.ok:
            body["steps"] = check.steps
        return body, check

    def validate(self, silent: bool = False) -> bool:
        if not self.key_var.get().strip():
            self._set_footer("请先填任务名称，或点「✨」自动生成 key", "#f0ad4e")
            return False
        if self._declared_total() == -1:
            self._set_footer("总步数得是正整数；留空表示不限步数", "#f0ad4e")
            return False
        if not self._steps:
            self._set_footer("还没有步骤：选类型 → 开始拾取，至少录一步", "#f0ad4e")
            return False
        _body, result = self._checked_body()
        if result.ok:
            self._set_footer(result.summary(), "#5cb85c")
            if not silent and (result.warnings or result.missing_templates):
                lines = list(result.warnings)
                if result.missing_templates:
                    lines.append("缺失模板：" + "、".join(result.missing_templates))
                messagebox.showinfo("校验通过，但有提醒", "\n".join(lines), parent=self)
            return True
        self._set_footer(result.summary(), "#e74c3c")
        if not silent:
            messagebox.showerror("校验未通过", "\n".join(result.errors), parent=self)
        return False

    def _resolve_target(self) -> str:
        raw = self.file_var.get().strip() or DEFAULT_FILE
        if not raw.endswith(".json"):
            raw += ".json"
        if os.path.isabs(raw):
            return raw
        if "/" in raw or os.sep in raw:
            return os.path.normpath(os.path.join(self.root_dir, raw))
        return os.path.normpath(os.path.join(self.root_dir, "tasks", raw))

    def save(self) -> bool:
        if not self.validate():
            return False
        key = self.key_var.get().strip()
        path = self._resolve_target()
        doc: dict = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    doc = json.load(fp) or {}
            except (OSError, json.JSONDecodeError) as exc:
                if not messagebox.askyesno(
                    "文件读取失败", f"{path}\n{exc}\n\n仍然覆盖写入？", parent=self
                ):
                    return False
                doc = {}

        body, _check = self._checked_body()
        doc = task_schema.upsert_task_document(doc, key, body, self.flow_var.get().strip())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            task_schema.save_tasks_document(path, doc)
        except OSError as exc:
            self._set_footer(f"写入失败：{exc}", "#e74c3c")
            return False

        others = [k for k in (doc.get("tasks") or {}) if k != key]
        self._set_footer(
            f"已写入 {os.path.relpath(path, self.root_dir)}（任务 {key}"
            + (f"，同文件另有 {len(others)} 个任务" if others else "")
            + "）。回主界面点「🔄 重载配置」即可看到。",
            "#5cb85c",
        )
        self._refresh_choices()
        return True

    def copy_json(self):
        if not self.validate(silent=True):
            return
        body, _check = self._checked_body()
        doc = task_schema.upsert_task_document(
            None, self.key_var.get().strip(), body, self.flow_var.get().strip()
        )
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_footer("任务 JSON 已复制到剪贴板", "#5cb85c")
        except Exception:
            self._set_footer("复制失败，可改用「💾 保存到 tasks/」", "#f0ad4e")

    # ================================================================== 试运行
    def dry_run(self):
        if not self.validate(silent=True):
            return
        key = self.key_var.get().strip()
        _body, check = self._checked_body()
        dialog = DryRunDialog(self, key)
        dialog.start(check, self.root_dir, self._frame_bgr)

    # ================================================================== 状态
    def _set_status(self, text: str, color: str = "gray"):
        self.status.configure(text=text, text_color=color)

    def _set_footer(self, text: str, color: str = "gray"):
        self.footer_status.configure(text=text, text_color=color)

    def _set_pick_status(self, text: str, color: str = "gray"):
        self.pick_status.configure(text=text, text_color=color)

    def destroy(self):
        try:
            self._source.close()
        finally:
            super().destroy()


class StepEditor(ctk.CTkToplevel):
    """单步 JSON 编辑器：双击步骤就能改坐标 / 加 times / 改 on_fail。"""

    def __init__(self, master, step: dict, on_save):
        super().__init__(master)
        self.title("编辑步骤")
        self.geometry("520x420")
        self.on_save = on_save
        self.transient(master)

        ctk.CTkLabel(
            self, text="直接改 JSON（可用方向键微调坐标）", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(anchor="w", padx=12, pady=(10, 4))

        self.box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Menlo", size=12), wrap="none")
        self.box.pack(fill="both", expand=True, padx=12, pady=4)
        self.box.insert("1.0", json.dumps(step, ensure_ascii=False, indent=2))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(bar, text="保存", width=90, command=self._save).pack(side="right")
        ctk.CTkButton(
            bar, text="取消", width=90, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self.destroy,
        ).pack(side="right", padx=6)

        self.after(120, self.focus_force)

    def _save(self):
        raw = self.box.get("1.0", "end").strip()
        try:
            step = json.loads(raw)
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON 有误", f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}", parent=self)
            return
        if not isinstance(step, dict):
            messagebox.showerror("JSON 有误", "一个步骤必须是一个对象 {...}", parent=self)
            return
        self.on_save(step)
        self.destroy()


class ChoiceDialog(ctk.CTkToplevel):
    """单选对话框（载入已有任务用）。"""

    def __init__(self, master, title: str, options: List[str]):
        super().__init__(master)
        self.title(title)
        self.geometry("500x440")
        self.result: Optional[str] = None
        self.transient(master)
        self.after(80, self.grab_set)

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(12, 6))
        box = ctk.CTkScrollableFrame(self)
        box.pack(fill="both", expand=True, padx=12, pady=6)
        for option in options:
            ctk.CTkButton(
                box,
                text=option,
                anchor="w",
                fg_color="transparent",
                border_width=1,
                text_color=("black", "white"),
                command=lambda o=option: self._choose(o),
            ).pack(fill="x", pady=2)
        ctk.CTkButton(self, text="取消", width=90, command=self.destroy).pack(pady=8)

    def _choose(self, option: str):
        self.result = option
        self.destroy()


class DryRunDialog(ctk.CTkToplevel):
    """试运行窗口：用演练后端把当前任务跑一遍，只在这里看日志。"""

    def __init__(self, master, key: str):
        super().__init__(master)
        self.title(f"试运行 · {key}")
        self.geometry("760x520")
        self.transient(master)
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._alive = True

        ctk.CTkLabel(
            self,
            text="演练模式：不产生真实点击，只验证步骤能否走通（模板缺失仍会失败）",
            text_color="gray",
        ).pack(anchor="w", padx=12, pady=(10, 4))
        self.box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Menlo", size=12))
        self.box.pack(fill="both", expand=True, padx=12, pady=4)
        self.box.configure(state="disabled")
        ctk.CTkButton(self, text="关闭", width=90, command=self.destroy).pack(pady=8)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(120, self._drain)

    def start(self, check, root_dir: str, frame=None):
        threading.Thread(
            target=self._worker, args=(check, root_dir, frame), daemon=True
        ).start()

    def push(self, line: str):
        self._queue.put(str(line))

    def _drain(self):
        if not self._alive:
            return
        lines = []
        try:
            while len(lines) < 200:
                lines.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.box.configure(state="normal")
            self.box.insert("end", "\n".join(lines) + "\n")
            self.box.see("end")
            self.box.configure(state="disabled")
        self.after(120, self._drain)

    def _worker(self, check, root_dir: str, frame):
        from core.backends import MockBackend
        from core.runner import Runner
        from core.task_schema import TaskDef, TaskSet

        logger = Logger(
            log_dir=os.path.join(root_dir, "logs"),
            echo=False,
            on_ui=self.push,
        )
        backend = None
        try:
            try:
                taskset = task_schema.load_tasks(
                    os.path.join(root_dir, "tasks"), base_dir=root_dir
                )
            except Exception:
                taskset = TaskSet(base_dir=root_dir)
            taskset.tasks[check.key] = TaskDef(
                key=check.key, desc=check.desc or check.key, steps=check.steps, source="<添加器>"
            )

            size = (1280, 720)
            if frame is not None and getattr(frame, "shape", None):
                h, w = frame.shape[:2]
                size = (int(w), int(h))
            backend = MockBackend(
                {"size": size}, logger, templates_dir=os.path.join(root_dir, "templates")
            )
            backend.connect()
            report = Runner(
                backend=backend,
                taskset=taskset,
                logger=logger,
                screenshot_on_fail=False,
            ).run([check.key])
            # 总结行由 Runner 通过 Logger.ui 推出，这里不重复
            if not report.ok:
                self.push("（详细原因见 logs/ 下的日志文件）")
        except Exception as exc:
            self.push(f"试运行失败：{type(exc).__name__}: {exc}")
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass

    def destroy(self):
        self._alive = False
        super().destroy()


class AdderWindow(ctk.CTkToplevel):
    def __init__(self, master, root: Optional[str] = None, mode: str = "screen", adb_config: Optional[dict] = None):
        super().__init__(master)
        self.title("任务添加器 · 这城有良田（开发者工具）")
        self.geometry("1280x860")
        self.minsize(1120, 760)
        self.panel = TaskAdder(self, root=root, mode=mode, adb_config=adb_config)
        self.panel.pack(fill="both", expand=True)
        self.after(250, self.focus_force)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="任务添加器（开发者工具）")
    parser.add_argument("--mode", choices=["screen", "adb"], default="screen")
    parser.add_argument("--adb", default="", help="adb 路径")
    parser.add_argument("--device", default="127.0.0.1:7555", help="设备地址")
    parser.add_argument("--window", default="", help="窗口标题（屏幕模式）")
    parser.add_argument("--file", default="", help="目标文件，默认 tasks/tasks_dev.json")
    parser.add_argument("--key", default="", help="任务 key")
    parser.add_argument("--desc", default="", help="任务显示名")
    args = parser.parse_args(argv)

    if not vision.CV_AVAILABLE:
        print("缺少 opencv-python / numpy，请先 pip install -r requirements.txt")
        return 1

    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    root.title("任务添加器 · 这城有良田")
    root.geometry("1280x860")
    panel = TaskAdder(
        root,
        mode=args.mode,
        adb_config={
            "adb_path": args.adb,
            "device": args.device,
            "window_title": args.window,
        },
        file=args.file,
        key=args.key,
        desc=args.desc,
    )
    panel.pack(fill="both", expand=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
