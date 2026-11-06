"""首页：任务计划 + 运行选项 + 运行控制 + 实时日志。

这里**不做任务勾选**——任务开关由 ``task_plan.json`` 决定（见 :mod:`core.task_plan`），
界面只负责把 JSON 的状态如实展示出来，并提供「导入 / 编辑 / 重新载入」三个动作。
好处是界面和文件永远一致，不会出现「界面勾了但文件没改」的两套状态。

版式：上方一排概览数字，下面左右两栏——左栏任务计划（宽），右栏运行选项 + 点位摘要（窄），
底部固定操作条与日志。刻意不做左侧导航栏，省掉一列。
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
from ui.widgets import Banner, Card, StatTile, Tag, set_entry_text

NOTIFY_LABELS = {
    "关闭": "none",
    "企业微信机器人": "wecom",
    "Server 酱": "serverchan",
    "自定义 Webhook": "webhook",
}
NOTIFY_KEYS = {value: label for label, value in NOTIFY_LABELS.items()}

# 左栏任务卡片的列数：15 个任务两列 8 行，一屏能看完
TASK_COLUMNS = 2


class HomePage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.manager = app.manager
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._alive = True
        self._flow_var = ctk.StringVar(value="")

        self._build()
        self.bind("<Destroy>", self._on_destroy)
        self.after(120, self._pump_logs)
        self.refresh()
        self._refresh_status(self.manager.status)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        # 底部两块先 pack，保证它们固定在窗口底部、不随内容滚动
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

        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=5)
        body.grid_columnconfigure(1, weight=4, minsize=330)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, T.GAP_MD))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")

        self._build_plan_card(left)
        self._build_run_card(right)
        self._build_points_card(right)
        self._build_templates_card(right)

    def _build_stats(self, parent):
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", pady=(0, T.GAP_MD))
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
        card.pack(fill="both", expand=True)
        self.plan_card = card

        T.ghost(card.actions, "重新载入", self.app.reload_plan, height=28, width=86).pack(
            side="right", padx=(6, 0)
        )
        T.ghost(card.actions, "编辑 JSON", self.app.open_plan_file, height=28, width=86).pack(
            side="right", padx=(6, 0)
        )
        T.ghost(card.actions, "导入", self.app.import_plan, height=28, width=60).pack(side="right")

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
            text="true 启用 / false 关闭，存盘后自动重载",
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
                f"计划里有 {len(unknown)} 个 key 在当前任务文件里不存在："
                f"{'、'.join(unknown[:4])}{'…' if len(unknown) > 4 else ''}",
                "warn",
            ).pack(fill="x", pady=(0, T.GAP_SM))

        self._render_tasks(plan, taskset)

    def _plan_location(self, plan: TaskPlan) -> str:
        """显示相对项目根的路径，避免整条绝对路径撑满一行。"""
        if not plan.path:
            return ""
        try:
            relative = os.path.relpath(plan.path, self.app.root_dir)
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
        for column in range(TASK_COLUMNS):
            grid.grid_columnconfigure(column, weight=1, uniform="task")

        for index, (key, task) in enumerate(tasks):
            enabled = plan.is_enabled(key, getattr(task, "enabled", True))
            row = self._task_row(grid, key, getattr(task, "desc", "") or key, enabled)
            row.grid(
                row=index // TASK_COLUMNS,
                column=index % TASK_COLUMNS,
                sticky="ew",
                padx=(0 if index % TASK_COLUMNS == 0 else T.GAP_SM, 0),
                pady=2,
            )

    def _task_row(self, parent, key: str, desc: str, enabled: bool) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=T.SURFACE_ALT, corner_radius=8, height=32)
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

    # ------------------------------------------------------------ 点位摘要卡
    def _build_points_card(self, parent):
        self.points_card = Card(parent, "点位选择", "改 JSON 的 points 字段")
        self.points_card.pack(fill="x", pady=(T.GAP_MD, 0))
        self._points_body = ctk.CTkFrame(self.points_card.body, fg_color="transparent")
        self._points_body.pack(fill="x")

    def _render_points(self):
        for widget in self._points_body.winfo_children():
            widget.destroy()

        groups = self.app.taskset.point_groups()
        if not groups:
            self.points_card.pack_forget()
            return
        if not self.points_card.winfo_manager():
            self.points_card.pack(fill="x", pady=(T.GAP_MD, 0))

        plan = self.app.plan
        total, picked = plan.point_stats(groups)
        summary = ctk.CTkFrame(self._points_body, fg_color="transparent")
        summary.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            summary,
            text=f"共 {len(groups)} 组点位，已勾 {picked}/{total}",
            font=T.font(T.SIZE_SMALL),
            text_color=T.TEXT_MUTED,
            anchor="w",
        ).pack(side="left")

        for group in groups:
            chosen = plan.chosen_points(group)
            row = ctk.CTkFrame(self._points_body, fg_color=T.SURFACE_ALT, corner_radius=8, height=30)
            row.pack_propagate(False)
            row.pack(fill="x", pady=2)

            name = group["task_title"]
            if group.get("step_name") and group["step_name"] != group["step_id"]:
                name = f"{name} · {group['step_name']}"
            ctk.CTkLabel(
                row,
                text=name,
                font=T.font(T.SIZE_SMALL),
                text_color=T.TEXT_MUTED if not chosen else T.TEXT,
                anchor="w",
            ).pack(side="left", padx=(10, 6), pady=5)
            ctk.CTkLabel(
                row,
                text=f"{len(chosen)}/{len(group['items'])}",
                font=T.mono(T.SIZE_TINY),
                text_color=T.SUCCESS if chosen else T.WARN,
            ).pack(side="right", padx=(4, 10))

    # ------------------------------------------------------------ 模板状态卡
    def _build_templates_card(self, parent):
        self.templates_card = Card(parent, "模板图", "缺图会让相关步骤失败")
        self.templates_card.pack(fill="x", pady=(T.GAP_MD, 0))
        self._templates_body = ctk.CTkFrame(self.templates_card.body, fg_color="transparent")
        self._templates_body.pack(fill="x")

    def _render_templates(self):
        for widget in self._templates_body.winfo_children():
            widget.destroy()

        missing = self.app.taskset.missing_templates
        if not missing:
            ctk.CTkLabel(
                self._templates_body,
                text="✔  模板图齐全",
                font=T.font(T.SIZE_SMALL),
                text_color=T.SUCCESS,
                anchor="w",
            ).pack(anchor="w")
            return

        first = missing[0]
        ctk.CTkLabel(
            self._templates_body,
            text=f"缺失 {len(missing)} 张，如 {first}"
            + (" 等" if len(missing) > 1 else "")
            + "；相关步骤会失败，用任务添加器补拍。",
            font=T.font(T.SIZE_SMALL),
            text_color=T.WARN,
            anchor="w",
            justify="left",
            wraplength=320,
        ).pack(anchor="w")

    # ------------------------------------------------------------ 运行选项卡
    def _build_run_card(self, parent):
        card = Card(parent, "运行选项", "只影响这一次运行")
        card.pack(fill="x")

        form = ctk.CTkFrame(card.body, fg_color="transparent")
        form.pack(fill="x")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            form, text="流程", anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).grid(row=0, column=0, sticky="w", pady=4)
        self.flow_menu = T.option_menu(form, ["—"], self._flow_var, width=140)
        self.flow_menu.grid(row=0, column=1, sticky="w", pady=4)

        ctk.CTkLabel(
            form, text="兑换码", anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.redeem_entry = T.entry(form, placeholder_text="留空不兑换")
        self.redeem_entry.grid(row=1, column=1, sticky="ew", pady=4)

        ctk.CTkLabel(
            form, text="通知", anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).grid(row=2, column=0, sticky="nw", pady=(4, 2))
        notify_cfg = self.app.config.get("notify") or {}
        self.notify_menu = T.option_menu(
            form, list(NOTIFY_LABELS), width=140, command=self._on_notify_change
        )
        self.notify_menu.set(NOTIFY_KEYS.get(notify_cfg.get("type", "none"), "关闭"))
        self.notify_menu.grid(row=2, column=1, sticky="w", pady=(4, 2))

        self.notify_entry = T.entry(form, placeholder_text="Webhook 地址 / SendKey")
        self.notify_entry.grid(row=3, column=1, sticky="ew", pady=(0, 4))
        self._on_notify_change(self.notify_menu.get())
        set_entry_text(
            self.notify_entry,
            notify_cfg.get("url") or notify_cfg.get("sendkey") or "",
            "Webhook 地址",
        )

        self.dry_run_var = ctk.BooleanVar(value=bool(self.app.config.get("dry_run", False)))
        T.switch(form, "仅演练（不真实点击）", self.dry_run_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 2)
        )

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
            height=70,
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
        self.tile_tasks.set(f"{len(enabled)}/{len(taskset.tasks)}")

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

        variables = dict(self.app.config.get("variables") or {})
        variables.update(plan.variables)
        set_entry_text(
            self.redeem_entry,
            variables.get("redeem_code", ""),
            "留空不兑换",
        )

        self._render_plan()
        self._render_points()
        self._render_templates()
        self._refresh_run_hint(enabled)

    def _refresh_run_hint(self, enabled: Optional[List[str]] = None):
        if enabled is None:
            enabled = self.app.plan.enabled_keys(self.app.taskset)
        flow = self._flow_var.get()
        self.run_hint.configure(text=f"流程 {flow or '—'} · 将执行 {len(enabled)} 个任务")

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
