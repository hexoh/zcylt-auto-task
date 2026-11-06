"""首页：任务计划 + 运行选项 + 运行控制 + 实时日志。

这里**不做任务勾选**——任务开关由 ``task_plan.json`` 决定（见 :mod:`core.task_plan`），
界面只负责把 JSON 的状态如实展示出来，并提供「导入 / 编辑 / 重新载入」三个动作。
好处是界面和文件永远一致，不会出现「界面勾了但文件没改」的两套状态。
"""

from __future__ import annotations

import os
import queue
from datetime import datetime
from typing import Dict, List, Optional

import customtkinter as ctk

from core.features import is_enabled, is_visible
from core.task_manager import TaskStatus
from core.task_plan import TaskPlan
from ui import theme as T
from ui.widgets import Banner, Card, StatTile, Tag

NOTIFY_LABELS = {
    "关闭": "none",
    "企业微信机器人": "wecom",
    "Server 酱": "serverchan",
    "自定义 Webhook": "webhook",
}
NOTIFY_KEYS = {value: label for label, value in NOTIFY_LABELS.items()}


class HomePage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.manager = app.manager
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._alive = True
        self._flow_var = ctk.StringVar(value="")
        self._notice_hint = ""

        self._build()
        self.bind("<Destroy>", self._on_destroy)
        self.after(120, self._pump_logs)
        self.refresh()
        self._refresh_status(self.manager.status)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        # 底部两块先 pack，保证它们固定、不随内容滚动
        self._build_log_card()
        self._build_action_bar()
        self._build_scroll()

    def _build_scroll(self):
        self._scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=T.BORDER_STRONG,
            scrollbar_button_hover_color=T.NEUTRAL,
        )
        self._scroll.pack(fill="both", expand=True, padx=T.PAGE_PAD - 8, pady=(T.GAP_SM, 0))

        inner = ctk.CTkFrame(self._scroll, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=4, pady=(2, 0))

        self._build_stats(inner)
        self._build_plan_card(inner)
        self._build_run_card(inner)

    def _build_stats(self, parent):
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", pady=(2, T.GAP_MD))
        for column in range(4):
            grid.grid_columnconfigure(column, weight=1, uniform="stat")

        self.tile_tasks = StatTile(grid, "启用任务 / 总数")
        self.tile_flow = StatTile(grid, "当前流程")
        self.tile_templates = StatTile(grid, "缺失模板图")
        self.tile_mode = StatTile(grid, "运行方案")
        for index, tile in enumerate(
            (self.tile_tasks, self.tile_flow, self.tile_templates, self.tile_mode)
        ):
            tile.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(0 if index == 0 else T.GAP_SM, 0),
            )

    # ------------------------------------------------------------ 任务计划卡
    def _build_plan_card(self, parent):
        card = Card(parent, "任务计划", "开关与点位都写在 JSON 里")
        self.plan_card = card

        T.ghost(
            card.actions, "重新载入", self.app.reload_plan, height=28, width=86
        ).pack(side="right", padx=(6, 0))
        T.ghost(
            card.actions, "编辑 JSON", self.app.open_plan_file, height=28, width=86
        ).pack(side="right", padx=(6, 0))
        T.ghost(
            card.actions, "导入", self.app.import_plan, height=28, width=64
        ).pack(side="right")

        self._plan_body = ctk.CTkFrame(card.body, fg_color="transparent")
        self._plan_body.pack(fill="x")

    def _render_plan(self):
        for widget in self._plan_body.winfo_children():
            widget.destroy()

        plan: TaskPlan = self.app.plan
        taskset = self.app.taskset

        meta = ctk.CTkFrame(self._plan_body, fg_color="transparent")
        meta.pack(fill="x", pady=(0, T.GAP_SM))
        ctk.CTkLabel(
            meta,
            text=self._plan_location(plan),
            font=T.mono(T.SIZE_TINY),
            text_color=T.TEXT_MUTED,
        ).pack(side="left")
        ctk.CTkLabel(
            meta,
            text="改 true / false 存盘后自动重新载入",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_FAINT,
        ).pack(side="left", padx=(10, 0))

        if getattr(self.app, "startup_error", ""):
            Banner(
                self._plan_body,
                f"任务定义加载失败：{self.app.startup_error}",
                "error",
            ).pack(fill="x", pady=(0, T.GAP_SM))

        if plan.error:
            Banner(self._plan_body, f"计划文件读不了：{plan.error}", "error").pack(
                fill="x", pady=(0, T.GAP_SM)
            )
        elif not plan.exists:
            Banner(
                self._plan_body,
                "还没有计划文件，已按当前任务定义生成一份。改里面的 true / false 即可控制开关。",
                "warn",
            ).pack(fill="x", pady=(0, T.GAP_SM))

        unknown = plan.unknown_task_keys(taskset)
        if unknown and not plan.error:
            Banner(
                self._plan_body,
                f"计划里有 {len(unknown)} 个任务 key 在当前任务文件里不存在："
                f"{'、'.join(unknown[:5])}{'…' if len(unknown) > 5 else ''}",
                "warn",
            ).pack(fill="x", pady=(0, T.GAP_SM))

        self._render_tasks(plan, taskset)
        self._render_points(plan, taskset)

    def _plan_location(self, plan: TaskPlan) -> str:
        """显示相对项目根的路径，避免整条绝对路径撑满一行。"""
        if not plan.path:
            return ""
        root = self.app.root_dir
        try:
            relative = os.path.relpath(plan.path, root)
        except ValueError:
            return plan.path
        return plan.path if relative.startswith("..") else relative

    def _render_tasks(self, plan: TaskPlan, taskset):
        tasks = list(taskset.tasks.items())
        if not tasks:
            Banner(
                self._plan_body,
                "没有加载到任务定义，请检查 tasks/ 目录或计划里的 tasks_path。",
                "error",
            ).pack(fill="x")
            return

        grid = ctk.CTkFrame(self._plan_body, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(0, weight=1, uniform="task")
        grid.grid_columnconfigure(1, weight=1, uniform="task")

        for index, (key, task) in enumerate(tasks):
            enabled = plan.is_enabled(key, getattr(task, "enabled", True))
            row = self._task_row(grid, key, getattr(task, "desc", "") or key, enabled)
            row.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=(0 if index % 2 == 0 else T.GAP_SM, 0),
                pady=3,
            )

    def _task_row(self, parent, key: str, desc: str, enabled: bool) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=T.SURFACE_ALT, corner_radius=8, height=36)
        row.pack_propagate(False)

        ctk.CTkLabel(
            row,
            text="●",
            width=12,
            font=T.font(T.SIZE_TINY),
            text_color=T.SUCCESS if enabled else T.TEXT_FAINT,
        ).pack(side="left", padx=(11, 7), pady=8)
        ctk.CTkLabel(
            row,
            text=desc,
            font=T.font(T.SIZE_BODY),
            text_color=T.TEXT if enabled else T.TEXT_FAINT,
            anchor="w",
        ).pack(side="left")
        Tag(
            row,
            "启用" if enabled else "关闭",
            fg=T.SUCCESS_SOFT if enabled else T.NEUTRAL_SOFT,
            color=T.SUCCESS if enabled else T.TEXT_FAINT,
        ).pack(side="right", padx=(4, 10))
        if key != desc:
            ctk.CTkLabel(
                row,
                text=key,
                font=T.mono(T.SIZE_TINY),
                text_color=T.TEXT_FAINT,
                anchor="e",
            ).pack(side="right", padx=(6, 0))
        return row

    def _render_points(self, plan: TaskPlan, taskset):
        groups = taskset.point_groups()
        if not groups:
            return

        total, picked = plan.point_stats(groups)
        head = ctk.CTkFrame(self._plan_body, fg_color="transparent")
        head.pack(fill="x", pady=(T.GAP_MD, 4))
        ctk.CTkLabel(
            head,
            text="点位选择",
            font=T.font(T.SIZE_BODY, "bold"),
            text_color=T.TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="改 JSON 的 points 字段，格式 任务key::步骤id → 点位 id 数组",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_FAINT,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(
            head,
            text=f"共 {len(groups)} 组 · 已勾 {picked}/{total}",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_MUTED,
        ).pack(side="right")

        grid = ctk.CTkFrame(self._plan_body, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(0, weight=1, uniform="point")
        grid.grid_columnconfigure(1, weight=1, uniform="point")
        for index, group in enumerate(groups):
            chosen = plan.chosen_points(group)
            row = ctk.CTkFrame(grid, fg_color=T.SURFACE_ALT, corner_radius=8, height=32)
            row.pack_propagate(False)
            name = group["task_title"]
            if group.get("step_name") and group["step_name"] != group["step_id"]:
                name = f"{name} · {group['step_name']}"
            ctk.CTkLabel(
                row,
                text=name,
                font=T.font(T.SIZE_SMALL),
                text_color=T.TEXT_MUTED if not chosen else T.TEXT,
                anchor="w",
            ).pack(side="left", padx=(11, 8), pady=6)
            ctk.CTkLabel(
                row,
                text=f"{len(chosen)}/{len(group['items'])}",
                font=T.mono(T.SIZE_TINY),
                text_color=T.SUCCESS if chosen else T.WARN,
            ).pack(side="right", padx=(4, 11))
            row.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=(0 if index % 2 == 0 else T.GAP_SM, 0),
                pady=3,
            )

    # ------------------------------------------------------------ 运行选项卡
    def _build_run_card(self, parent):
        card = Card(parent, "运行选项", "只影响这一次运行，不改任务开关")

        row1 = ctk.CTkFrame(card.body, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            row1, text="流程", width=52, anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).pack(side="left")
        self.flow_menu = T.option_menu(row1, ["—"], self._flow_var, width=150)
        self.flow_menu.pack(side="left")
        ctk.CTkLabel(
            row1, text="兑换码", width=64, anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).pack(side="left", padx=(18, 0))
        self.redeem_entry = T.entry(row1, placeholder_text="redeem_code，留空则不兑换")
        self.redeem_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))

        row2 = ctk.CTkFrame(card.body, fg_color="transparent")
        row2.pack(fill="x", pady=(6, 0))
        self.dry_run_var = ctk.BooleanVar(value=bool(self.app.config.get("dry_run", False)))
        T.switch(row2, "仅演练（不真实点击）", self.dry_run_var).pack(side="left")
        ctk.CTkLabel(
            row2, text="通知", width=52, anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).pack(side="left", padx=(22, 0))
        notify_cfg = self.app.config.get("notify") or {}
        self.notify_menu = T.option_menu(row2, list(NOTIFY_LABELS), width=140, command=self._on_notify_change)
        self.notify_menu.set(NOTIFY_KEYS.get(notify_cfg.get("type", "none"), "关闭"))
        self.notify_menu.pack(side="left")
        self.notify_entry = T.entry(row2, placeholder_text="Webhook 地址 / Server 酱 SendKey")
        self.notify_entry.pack(side="left", fill="x", expand=True, padx=(8, 2))
        self.notify_entry.insert(0, str(notify_cfg.get("url") or notify_cfg.get("sendkey") or ""))
        self._on_notify_change(self.notify_menu.get())

    # ------------------------------------------------------------ 操作条 / 日志
    def _build_action_bar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=T.PAGE_PAD, pady=(T.GAP_SM, T.GAP_SM))

        self.start_btn = T.primary(bar, "▶  开始运行", self._on_start, width=142, height=40)
        self.start_btn.pack(side="left")
        self.stop_btn = T.danger(bar, "■  停止", self._on_stop, width=104, height=40)
        self.stop_btn.pack(side="left", padx=(T.GAP_SM, 0))

        self.run_hint = ctk.CTkLabel(
            bar, text="", font=T.font(T.SIZE_SMALL), text_color=T.TEXT_MUTED, anchor="e"
        )
        self.run_hint.pack(side="right")

    def _build_log_card(self):
        card = Card(self, "运行日志", "界面只显示结果，细节写进 logs/ 下的文件")
        self.log_card = card

        T.text_button(card.actions, "清空", self._clear_log).pack(side="right", padx=(4, 0))
        if is_visible(self.app.config, "tools", "logs"):
            T.text_button(
                card.actions,
                "日志目录",
                lambda: self.app.open_dir(os.path.join(self.app.root_dir, "logs")),
                state="normal" if is_enabled(self.app.config, "tools", "logs") else "disabled",
            ).pack(side="right", padx=(4, 0))

        self.log_box = ctk.CTkTextbox(
            card.body,
            height=116,
            font=T.mono(11),
            fg_color=T.SURFACE_ALT,
            text_color=T.TEXT,
            border_width=0,
            corner_radius=8,
            wrap="none",
        )
        self.log_box.pack(fill="x")
        self.log_box.configure(state="disabled")
        card.pack(side="bottom", fill="x", padx=T.PAGE_PAD, pady=(0, T.GAP_SM))

    # ------------------------------------------------------------------ 刷新
    def refresh(self):
        plan: TaskPlan = self.app.plan
        taskset = self.app.taskset

        enabled = plan.enabled_keys(taskset)
        total = len(taskset.tasks)
        self.tile_tasks.set(f"{len(enabled)}/{total}")

        flows = taskset.flow_names()
        preferred = plan.flow or self.app.config.get("flow") or (flows[0] if flows else "")
        self.flow_menu.configure(values=flows or ["—"])
        if preferred in flows or not flows:
            self._flow_var.set(preferred or "—")
        elif not self._flow_var.get():
            self._flow_var.set(flows[0])
        self.tile_flow.set(self._flow_var.get() or "—")

        missing = len(taskset.missing_templates)
        self.tile_templates.set(str(missing), T.WARN if missing else T.SUCCESS)

        self.tile_mode.set(self.app.mode_label())

        self.redeem_entry.delete(0, "end")
        variables = dict(self.app.config.get("variables") or {})
        variables.update(plan.variables)
        self.redeem_entry.insert(0, str(variables.get("redeem_code", "")))

        self._render_plan()
        self._refresh_run_hint(enabled)

    def _refresh_run_hint(self, enabled: Optional[List[str]] = None):
        if enabled is None:
            enabled = self.app.plan.enabled_keys(self.app.taskset)
        flow = self._flow_var.get()
        self.run_hint.configure(
            text=f"流程 {flow or '—'} · 将执行 {len(enabled)} 个任务"
        )

    def _on_notify_change(self, value: str):
        self.notify_entry.configure(
            placeholder_text="Server 酱 SendKey"
            if NOTIFY_LABELS.get(value) == "serverchan"
            else "Webhook 地址"
        )

    # ------------------------------------------------------------------ 配置
    def apply_to_config(self):
        """把「这一次运行」的选项写回内存配置（落盘由 App 负责）。"""
        notify_type = NOTIFY_LABELS.get(self.notify_menu.get(), "none")
        value = self.notify_entry.get().strip()
        notify: Dict[str, str] = {"type": notify_type}
        if notify_type == "serverchan":
            notify["sendkey"] = value
        else:
            notify["url"] = value
        self.app.config["notify"] = notify
        self.app.config["flow"] = self._flow_var.get()
        self.app.config["dry_run"] = bool(self.dry_run_var.get())
        variables = dict(self.app.config.get("variables") or {})
        variables["redeem_code"] = self.redeem_entry.get().strip()
        self.app.config["variables"] = variables

    def build_run_overrides(self) -> dict:
        return {
            "flow": self._flow_var.get(),
            "dry_run": bool(self.dry_run_var.get()),
            "variables": {"redeem_code": self.redeem_entry.get().strip()},
        }

    # ------------------------------------------------------------------ 运行
    def _on_start(self):
        if self.manager.is_running:
            self.append("任务已在运行")
            return
        config = self.app.build_run_config()
        if not config:
            return
        self.app.persist_config()
        ok, message = self.manager.start(config)
        if not ok:
            self.append(message)

    def _on_stop(self):
        if self.manager.is_running:
            self.manager.stop()

    # ------------------------------------------------------------------ 日志
    def append(self, line: str):
        """线程安全：后台线程也可以直接调用。"""
        text = str(line).rstrip()
        for prefix in ("[系统] ", "[系统]", "[错误] ", "[错误]"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if not text.startswith("["):
            text = f"[{datetime.now():%H:%M:%S}] {text}"
        self._log_queue.put(text)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _pump_logs(self):
        if not self._alive:
            return
        lines: List[str] = []
        try:
            while len(lines) < 200:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", "\n".join(lines) + "\n")
            self.log_box.see("end")
            if float(self.log_box.index("end-1c").split(".")[0]) > 800:
                self.log_box.delete("1.0", "400.0")
            self.log_box.configure(state="disabled")
        self.after(120, self._pump_logs)

    # ------------------------------------------------------------------ 状态
    def on_status(self, status: TaskStatus):
        self._refresh_status(status)

    def _refresh_status(self, status: TaskStatus):
        running = status != TaskStatus.IDLE
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if not running:
            self._refresh_run_hint()

    def _on_destroy(self, _event=None):
        self._alive = False
