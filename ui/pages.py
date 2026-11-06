"""两个方案的界面页：配置 + 任务勾选 + 运行控制 + 实时日志。

任务列表、流程下拉都从 JSON 动态生成，改 JSON 点「重载配置」即可，不用改代码。
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime
from typing import Dict, List

import customtkinter as ctk

from core.task_manager import TaskManager, TaskStatus
from core.task_schema import TaskSet

NOTIFY_LABELS = {
    "关闭": "none",
    "企业微信机器人": "wecom",
    "Server 酱": "serverchan",
    "自定义 Webhook": "webhook",
}
NOTIFY_KEYS = {v: k for k, v in NOTIFY_LABELS.items()}
CAPTURE_LABELS = {"mss（快速）": "mss", "PIL（兼容）": "pil"}


class BasePage(ctk.CTkFrame):
    mode_name = "adb"
    page_title = ""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.manager: TaskManager = app.manager
        self.taskset: TaskSet = app.taskset
        self.task_vars: Dict[str, ctk.BooleanVar] = {}
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._alive = True

        self._build_header()

        # 底部固定：控制条 + 日志
        self._build_log()
        self._build_controls()
        self._build_body()

        self.bind("<Destroy>", self._on_destroy)
        self.after(120, self._pump_logs)
        self._refresh_status()

    # ------------------------------------------------------------------ 布局
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(side="top", fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            head, text=self.page_title, font=ctk.CTkFont(size=18, weight="bold")
        ).pack(side="left")
        self.hint_label = ctk.CTkLabel(head, text="", text_color="gray")
        self.hint_label.pack(side="right")

    def _build_body(self):
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(side="top", fill="both", expand=True, padx=8, pady=4)
        self._build_settings()
        self._build_runtime_options()
        self._build_task_list()
        self._build_point_list()

    def _build_log(self):
        wrap = ctk.CTkFrame(self)
        wrap.pack(side="bottom", fill="x", padx=14, pady=(4, 10))
        ctk.CTkLabel(wrap, text="运行日志", font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=10, pady=(6, 0)
        )
        self.log_box = ctk.CTkTextbox(wrap, height=150, font=ctk.CTkFont(family="Menlo", size=11))
        self.log_box.pack(fill="x", padx=8, pady=(4, 8))
        self.log_box.configure(state="disabled")

    def _build_controls(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=14, pady=(6, 0))

        self.start_btn = ctk.CTkButton(bar, text="▶ 开始运行", command=self._on_start, width=130)
        self.start_btn.pack(side="left")

        self.stop_btn = ctk.CTkButton(
            bar,
            text="⏹ 停止",
            command=self._on_stop,
            width=100,
            state="disabled",
            fg_color="#D9534F",
            hover_color="#C9302C",
        )
        self.stop_btn.pack(side="left", padx=8)

        ctk.CTkButton(
            bar,
            text="🔌 测试连接",
            command=self._on_probe,
            width=110,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            bar,
            text="🔄 重载配置",
            command=self.app.reload_tasks,
            width=110,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
        ).pack(side="left", padx=4)

        self.status_label = ctk.CTkLabel(bar, text="● 空闲", text_color="gray")
        self.status_label.pack(side="right")

    # ------------------------------------------------------------------ 卡片
    def _card(self, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.body)
        frame.pack(fill="x", padx=6, pady=6)
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 4)
        )
        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=(0, 8))
        return inner

    def _build_settings(self):
        raise NotImplementedError

    def _build_runtime_options(self):
        card = self._card("运行选项")

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", pady=3)
        ctk.CTkLabel(row1, text="流程：", width=70, anchor="w").pack(side="left")
        flows = self.taskset.flow_names()
        self.flow_var = ctk.StringVar(value=flows[0] if flows else "daily")
        self.flow_menu = ctk.CTkOptionMenu(
            row1, values=flows or ["（无流程）"], variable=self.flow_var, width=180
        )
        self.flow_menu.pack(side="left")
        preferred = self.app.config.get("flow")
        if preferred in flows:
            self.flow_var.set(preferred)
        ctk.CTkLabel(row1, text="  兑换码：", anchor="w").pack(side="left")
        self.redeem_entry = ctk.CTkEntry(row1, width=180, placeholder_text="变量 redeem_code")
        self.redeem_entry.pack(side="left")
        self.redeem_entry.insert(0, str(self.app.config.get("variables", {}).get("redeem_code", "")))

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", pady=3)
        self.dry_run_var = ctk.BooleanVar(value=bool(self.app.config.get("dry_run", False)))
        ctk.CTkCheckBox(
            row2,
            text="仅演练（不真实点击，用拼贴画布验证流程）",
            variable=self.dry_run_var,
        ).pack(side="left")

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", pady=3)
        ctk.CTkLabel(row3, text="通知：", width=70, anchor="w").pack(side="left")
        notify_cfg = self.app.config.get("notify", {})
        self.notify_menu = ctk.CTkOptionMenu(
            row3,
            values=list(NOTIFY_LABELS),
            width=150,
            command=self._on_notify_change,
        )
        self.notify_menu.set(NOTIFY_KEYS.get(notify_cfg.get("type", "none"), "关闭"))
        self.notify_menu.pack(side="left")
        self.notify_entry = ctk.CTkEntry(
            row3, width=340, placeholder_text="Webhook 地址 / Server 酱 SendKey"
        )
        self.notify_entry.pack(side="left", padx=8)
        self.notify_entry.insert(
            0, str(notify_cfg.get("url") or notify_cfg.get("sendkey") or "")
        )

        self.template_hint = ctk.CTkLabel(
            card,
            text="",
            anchor="w",
            justify="left",
            wraplength=760,
            text_color="#f0ad4e",
        )
        self.template_hint.pack(fill="x", pady=(4, 0))
        self._refresh_template_hint()

    def _build_task_list(self):
        card = self._card("自动任务")
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            top, text="勾选要执行的任务（流程决定顺序）", text_color="gray"
        ).pack(side="left")
        ctk.CTkButton(
            top, text="全选", width=60, command=lambda: self._set_all(True),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            top, text="全不选", width=70, command=lambda: self._set_all(False),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right")

        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x")
        self.task_vars.clear()
        tasks = list(self.taskset.tasks.values())
        if not tasks:
            ctk.CTkLabel(grid, text="没有加载到任务，请检查 tasks/ 目录", text_color="#e74c3c").grid(
                row=0, column=0, sticky="w", padx=6, pady=4
            )
            return
        for i, task in enumerate(tasks):
            var = ctk.BooleanVar(value=task.enabled)
            label = task.desc or task.key
            if not task.enabled:
                label += "（已禁用）"
            cb = ctk.CTkCheckBox(grid, text=label, variable=var, width=220)
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=3)
            if not task.enabled:
                cb.configure(state="disabled")
            var.trace_add("write", lambda *_: self._sync_point_enabled())
            self.task_vars[task.key] = var

    def _set_all(self, value: bool):
        for task in self.taskset.tasks.values():
            if not task.enabled:
                continue
            self.task_vars[task.key].set(value)
        self._sync_point_enabled()

    # ------------------------------------------------------------------ 点位
    def _build_point_list(self):
        """把 JSON 里的 ``tap_list`` 步骤摊成「任务 → 步骤 → 点位」三级勾选。

        没勾的点位执行时直接跳过（`selection` 传给执行器），所以「20 个奖励只想领
        第 1、3 个」不用改 JSON。
        """
        self.point_vars = {}
        self.point_cbs = {}
        self.point_counts = {}
        groups = self.taskset.point_groups()
        if not groups:
            return

        saved = self.app.config.get("selection") or {}
        rows = sum(1 + (len(g["items"]) + 2) // 3 for g in groups)

        frame = ctk.CTkFrame(self.body)
        frame.pack(fill="x", padx=6, pady=6)
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(head, text="点位选择", font=ctk.CTkFont(size=14, weight="bold")).pack(
            side="left"
        )
        self.point_summary = ctk.CTkLabel(head, text="", text_color="gray")
        self.point_summary.pack(side="left", padx=8)
        ctk.CTkButton(
            head, text="全不选", width=66, height=24, command=lambda: self._set_all_points(False),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right")
        ctk.CTkButton(
            head, text="全选", width=56, height=24, command=lambda: self._set_all_points(True),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right", padx=4)

        ctk.CTkLabel(
            frame,
            text="列表类奖励按点位勾选：没勾的会被跳过，不会误点；点「全不选」则整步跳过。",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=14, pady=(0, 4))

        if rows > 6:
            container = ctk.CTkScrollableFrame(frame, height=190, fg_color="transparent")
        else:
            container = ctk.CTkFrame(frame, fg_color="transparent")
        container.pack(fill="x", padx=6, pady=(0, 8))

        for group in groups:
            self._build_point_group(container, group, saved)

        self._sync_point_enabled()

    def _build_point_group(self, parent, group: dict, saved: dict):
        key = group["key"]
        items = group["items"]
        saved_items = saved.get(key)
        if isinstance(saved_items, (list, tuple)):
            chosen = {str(v) for v in saved_items}
        elif group["default_selected"]:
            chosen = {str(item["id"]) for item in items}
        else:
            chosen = set()

        box = ctk.CTkFrame(parent, fg_color=("gray90", "gray20"), corner_radius=8)
        box.pack(fill="x", padx=4, pady=3)

        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(6, 2))

        title = group["task_title"]
        step_name = group["step_name"]
        if step_name and step_name != group["step_id"]:
            title = f"{title} · {step_name}"
        ctk.CTkLabel(top, text=title, anchor="w", font=ctk.CTkFont(size=13, weight="bold")).pack(
            side="left"
        )
        count = ctk.CTkLabel(top, text="", text_color="gray", font=ctk.CTkFont(size=11))
        count.pack(side="left", padx=8)
        ctk.CTkButton(
            top, text="全不选", width=56, height=22, command=lambda k=key: self._set_group(k, False),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right")
        ctk.CTkButton(
            top, text="全选", width=50, height=22, command=lambda k=key: self._set_group(k, True),
            fg_color="transparent", border_width=1, text_color=("black", "white"),
        ).pack(side="right", padx=4)

        grid = ctk.CTkFrame(box, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(0, 6))

        self.point_vars[key] = {}
        self.point_cbs[key] = []
        self.point_counts[key] = count
        for i, item in enumerate(items):
            item_id = str(item["id"])
            var = ctk.BooleanVar(value=item_id in chosen)
            cb = ctk.CTkCheckBox(
                grid,
                text=str(item.get("name") or item_id),
                variable=var,
                width=190,
                checkbox_width=18,
                checkbox_height=18,
                font=ctk.CTkFont(size=12),
            )
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 8), pady=2)
            var.trace_add("write", lambda *_, k=key: self._refresh_point_count(k))
            self.point_vars[key][item_id] = var
            self.point_cbs[key].append(cb)

        self._refresh_point_count(key)

    def _set_group(self, key: str, value: bool):
        for var in self.point_vars.get(key, {}).values():
            var.set(value)

    def _set_all_points(self, value: bool):
        for key in self.point_vars:
            self._set_group(key, value)

    def _refresh_point_count(self, key: str):
        label = self.point_counts.get(key)
        if label is None:
            return
        vars_ = self.point_vars.get(key, {})
        picked = sum(1 for v in vars_.values() if v.get())
        text = f"已选 {picked}/{len(vars_)}"
        label.configure(text=text, text_color=("gray40", "gray70") if picked else "#e08b00")
        self._refresh_point_summary()

    def _refresh_point_summary(self):
        label = getattr(self, "point_summary", None)
        if label is None:
            return
        total = sum(len(v) for v in self.point_vars.values())
        picked = sum(1 for vs in self.point_vars.values() for v in vs.values() if v.get())
        label.configure(text=f"共 {total} 个点位，已勾 {picked} 个")

    def _sync_point_enabled(self):
        """任务没勾选时，它的点位区置灰——避免「勾了点位却因为任务没勾没跑」。"""
        for key, cbs in getattr(self, "point_cbs", {}).items():
            task_key = key.split("::", 1)[0]
            var = self.task_vars.get(task_key)
            state = "normal" if (var is None or var.get()) else "disabled"
            for cb in cbs:
                cb.configure(state=state)

    def collect_selection(self) -> Dict[str, List[str]]:
        """界面上的点位勾选 → 执行器的 ``selection``。"""
        return {
            key: [item_id for item_id, var in vars_.items() if var.get()]
            for key, vars_ in getattr(self, "point_vars", {}).items()
        }

    def _refresh_template_hint(self):
        missing = self.taskset.missing_templates
        if missing:
            head = f"⚠️ 缺失 {len(missing)} 张模板图，相关步骤会失败："
            preview = "、".join(missing[:4]) + ("…" if len(missing) > 4 else "")
            self.template_hint.configure(text=f"{head}{preview}  → 用「任务添加器」补拍")
        else:
            self.template_hint.configure(text="✔ 模板图齐全")

    def _on_notify_change(self, value: str):
        self.notify_entry.configure(
            placeholder_text="Server 酱 SendKey"
            if NOTIFY_LABELS.get(value) == "serverchan"
            else "Webhook 地址"
        )

    # ------------------------------------------------------------------ 配置
    def collect_backend_config(self) -> dict:
        raise NotImplementedError

    def collect_config(self) -> dict:
        notify_type = NOTIFY_LABELS.get(self.notify_menu.get(), "none")
        entry = self.notify_entry.get().strip()
        notify = {"type": notify_type}
        if notify_type == "serverchan":
            notify["sendkey"] = entry
        else:
            notify["url"] = entry

        selected: List[str] = [k for k, v in self.task_vars.items() if v.get()]

        variables = dict(self.app.config.get("variables") or {})
        variables["redeem_code"] = self.redeem_entry.get().strip()

        return {
            "mode": "mock" if self.dry_run_var.get() else self.mode_name,
            "backend": self.collect_backend_config(),
            "flow": self.flow_var.get(),
            "flows": [self.flow_var.get()],
            "task_keys": selected,
            "selection": self.collect_selection(),
            "variables": variables,
            "notify": notify,
            "dry_run": bool(self.dry_run_var.get()),
            "screenshot_on_fail": True,
        }

    # ------------------------------------------------------------------ 事件
    def _on_start(self):
        if self.manager.is_running:
            self.append("任务已在运行")
            return
        config = self.collect_config()
        if not config["task_keys"]:
            self.app.error("没有勾选任务", "请至少勾选一个要执行的任务")
            return
        if config["mode"] == "mock":
            self.append("演练模式：不会产生真实点击")
        self.app.persist_config(self)
        ok, msg = self.manager.start(config)
        if not ok:
            self.append(msg)

    def _on_stop(self):
        if not self.manager.is_running:
            return
        self.manager.stop()

    def _on_probe(self):
        config = self.collect_config()
        if config["mode"] == "mock":
            config["mode"] = self.mode_name
        self.append(f"正在测试 {self.mode_name} 连接…")

        def worker():
            from core.task_manager import probe
            from core.logger import Logger

            logger = Logger(
                log_dir=os.path.join(self.app.root_dir, "logs"),
                on_ui=self.append,
                level="info",
            )
            ok, msg, path = probe(
                {
                    "mode": config["mode"],
                    "backend": config["backend"],
                    "templates_dir": os.path.join(self.app.root_dir, "templates"),
                },
                logger,
            )
            self.append(("连接成功：" if ok else "连接失败：") + msg)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ 日志
    def append(self, line: str):
        """线程安全：任何线程都可以调用。

        界面日志统一成 ``[HH:MM:SS] 中文短句``：没有等级、没有任务 key。
        传进来的行如果没带时间戳就补一个；历史遗留的 ``[系统]`` 前缀会被去掉。
        """
        text = str(line).rstrip()
        for prefix in ("[系统] ", "[系统]", "[错误] ", "[错误]"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        if not text.startswith("["):
            text = f"[{datetime.now():%H:%M:%S}] {text}"
        self._log_queue.put(text)

    def _pump_logs(self):
        if not self._alive:
            return
        lines = []
        try:
            while len(lines) < 200:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", "\n".join(lines) + "\n")
            self.log_box.see("end")
            if float(self.log_box.index("end-1c").split(".")[0]) > 2000:
                self.log_box.delete("1.0", "600.0")
            self.log_box.configure(state="disabled")
        self.after(120, self._pump_logs)

    # ------------------------------------------------------------------ 状态
    def on_status(self, status: TaskStatus):
        self._refresh_status(status)

    def _refresh_status(self, status=None):
        status = status or self.manager.status
        if status == TaskStatus.RUNNING:
            text, color = "● 运行中", "#5cb85c"
        elif status == TaskStatus.STARTING:
            text, color = "● 启动中…", "#f0ad4e"
        elif status == TaskStatus.STOPPING:
            text, color = "● 停止中…", "#f0ad4e"
        else:
            text, color = "● 空闲", "gray"
        self.status_label.configure(text=text, text_color=color)
        running = status != TaskStatus.IDLE
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _on_destroy(self, _event=None):
        self._alive = False


class ADBPage(BasePage):
    mode_name = "adb"
    page_title = "📱 方案一：Android ADB（模拟器）"

    def _build_settings(self):
        card = self._card("ADB 配置")
        cfg = self.app.config.get("adb", {})

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", pady=3)
        ctk.CTkLabel(row1, text="ADB 路径：", width=70, anchor="w").pack(side="left")
        self.adb_path = ctk.CTkEntry(row1, placeholder_text="留空用系统 PATH，或填完整路径")
        self.adb_path.pack(side="left", fill="x", expand=True)
        self.adb_path.insert(0, str(cfg.get("adb_path") or ""))

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", pady=3)
        ctk.CTkLabel(row2, text="设备地址：", width=70, anchor="w").pack(side="left")
        self.device_addr = ctk.CTkEntry(row2, placeholder_text="127.0.0.1:7555（MuMu）")
        self.device_addr.pack(side="left", fill="x", expand=True)
        self.device_addr.insert(0, str(cfg.get("device") or ""))

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", pady=3)
        ctk.CTkLabel(row3, text="分辨率：", width=70, anchor="w").pack(side="left")
        self.resolution = ctk.CTkEntry(row3, width=140, placeholder_text="留空自动获取")
        self.resolution.pack(side="left")
        ctk.CTkLabel(
            row3, text="   填 宽x高（如 1280x720）；留空则从 wm size 读取", text_color="gray"
        ).pack(side="left")

    def collect_backend_config(self) -> dict:
        return {
            "adb_path": self.adb_path.get().strip(),
            "device": self.device_addr.get().strip(),
            "resolution": self.resolution.get().strip(),
        }


class ScreenCapturePage(BasePage):
    mode_name = "screen"
    page_title = "🖥️ 方案二：屏幕截取 + 模拟键鼠（微信小程序）"

    def _build_settings(self):
        card = self._card("窗口配置")
        cfg = self.app.config.get("screen", {})

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", pady=3)
        ctk.CTkLabel(row1, text="窗口标题：", width=70, anchor="w").pack(side="left")
        self.window_title = ctk.CTkEntry(row1, placeholder_text="这城有良田 / WeChat")
        self.window_title.pack(side="left", fill="x", expand=True)
        self.window_title.insert(0, str(cfg.get("window_title") or ""))

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", pady=3)
        ctk.CTkLabel(row2, text="采集模式：", width=70, anchor="w").pack(side="left")
        self.capture_mode = ctk.CTkOptionMenu(row2, values=list(CAPTURE_LABELS), width=150)
        self.capture_mode.set(
            "PIL（兼容）" if cfg.get("capture_mode") == "pil" else "mss（快速）"
        )
        self.capture_mode.pack(side="left")
        ctk.CTkLabel(row2, text="   mss 更快；花屏/黑屏时换 PIL", text_color="gray").pack(side="left")

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", pady=3)
        ctk.CTkLabel(row3, text="固定区域：", width=70, anchor="w").pack(side="left")
        self.region = ctk.CTkEntry(row3, width=220, placeholder_text="x,y,w,h（可选，优先于标题）")
        self.region.pack(side="left")
        self.region.insert(0, str(cfg.get("region") or ""))
        ctk.CTkLabel(row3, text="   窗口找不到时填这里的绝对像素区域", text_color="gray").pack(side="left")

        row4 = ctk.CTkFrame(card, fg_color="transparent")
        row4.pack(fill="x", pady=3)
        self.follow_var = ctk.BooleanVar(value=bool(cfg.get("follow_window", True)))
        ctk.CTkCheckBox(row4, text="窗口移动时自动跟随（3 秒校准一次）", variable=self.follow_var).pack(
            side="left"
        )

    def collect_backend_config(self) -> dict:
        region_text = self.region.get().strip().replace("，", ",")
        region = None
        if region_text:
            parts = [p.strip() for p in region_text.split(",")]
            if len(parts) == 4 and all(p.lstrip("-").isdigit() for p in parts):
                region = [int(p) for p in parts]
        return {
            "window_title": self.window_title.get().strip(),
            "capture_mode": CAPTURE_LABELS.get(self.capture_mode.get(), "mss"),
            "region": region,
            "follow_window": bool(self.follow_var.get()),
        }
