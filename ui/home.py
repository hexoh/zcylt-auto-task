"""首页：任务计划 + 运行选项 + 运行控制 + 实时日志。

任务开关由 ``task_plan.json`` 决定（见 :mod:`core.task_plan`）：界面把 JSON 的状态如实
展示出来，并把每一行改成可点的开关——点了就走 ``App.set_plan_task`` 立刻写回文件。
所以「界面上的状态」和「文件里的状态」始终是同一份，不会出现谁覆盖谁；
外部用编辑器改完，1.5 秒内也会自动重载。

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
from ui.widgets import Banner, Card, StatTile, set_entry_text

# 左栏任务卡片的列数：15 个任务两列 8 行，一屏能看完
TASK_COLUMNS = 2
# 日志框高度（像素）：mono 11 单行实高约 15px，200 大约同屏 13 行，
# 一次跑完一个任务的结果基本能连着看完，不用为每一行都滚一下。
LOG_HEIGHT = 200
# 「启用」胶囊按钮的悬停色（比 SUCCESS_SOFT 深一档，点得下去看得见）
PILL_HOVER_ON = ("#D6F1E5", "#1E3A2D")


class HomePage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.manager = app.manager
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._alive = True
        self._flow_var = ctk.StringVar(value="")
        # 菜单里显示中文名，真正传给执行器的是流程 key
        self._flow_keys: Dict[str, str] = {}
        # 任务行里的「启用/停止」按钮等控件，key → {enabled, dot, title, pill}
        self._task_rows: Dict[str, dict] = {}
        # 「全部启用 / 全部停止」两个按钮，换计划时会重建
        self._bulk_buttons: List[ctk.CTkButton] = []

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
        self._build_watch_card(right)

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
        card = Card(parent, "任务计划", "点启用 / 停止即写回 JSON")
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
        self._template_footer = ctk.CTkFrame(card.body, fg_color="transparent")
        self._template_footer.pack(fill="x", pady=(6, 0))

    def _render_plan(self):
        for widget in self._plan_body.winfo_children():
            widget.destroy()
        # 旧行的控件随父容器一起销毁了，映射表也得清掉，避免指向已 destroy 的按钮
        self._task_rows = {}

        plan: TaskPlan = self.app.plan
        taskset = self.app.taskset

        meta = ctk.CTkFrame(self._plan_body, fg_color="transparent")
        meta.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            meta,
            text=self._plan_location(plan),
            font=T.mono(T.SIZE_TINY),
            text_color=T.TEXT_MUTED,
        ).pack(side="left")
        ctk.CTkLabel(
            meta,
            text="点右侧按钮或任务行即可切换",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_FAINT,
        ).pack(side="left", padx=(10, 0))

        # 「全部」两个动作放在同一行右侧：15 个任务逐个点太累，口径仍是写回 JSON
        bulk_state = "disabled" if self.manager.is_running else "normal"
        self._bulk_buttons = [
            T.text_button(
                meta, "全部停止", lambda: self._on_bulk_tasks(False),
                height=20, width=58, state=bulk_state,
            ),
            T.text_button(
                meta, "全部启用", lambda: self._on_bulk_tasks(True),
                height=20, width=58, state=bulk_state,
            ),
        ]
        for button in self._bulk_buttons:
            button.pack(side="right", padx=(4, 0))

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
                "还没有计划文件，已按当前任务定义生成一份。点右侧开关，或直接改里面的 true / false。",
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
        tasks = [
            (key, task)
            for key, task in taskset.tasks.items()
            if not getattr(task, "watch_only", False)
        ]
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
                pady=1,
            )

    def _task_row(self, parent, key: str, desc: str, enabled: bool) -> ctk.CTkFrame:
        """一行任务：状态点 + 中文名 + key + 启用/停止按钮。

        按钮对应的就是 ``task_plan.json → tasks[key]`` 那个 true/false：
        点一下走 :meth:`App.set_plan_task` 立刻落盘，行内其它零件只跟着变色，
        不在界面里另存一份状态（避免「界面开着、文件关着」）。

        用带文字的胶囊按钮而不是 CTkSwitch：本机 Tk 9.0 + CTk 6.0 下 switch 的
        白色滑块会盖住轨道右半，栏位里看成一个「D」形，读不出开/关。
        """
        row = ctk.CTkFrame(parent, fg_color=T.SURFACE_ALT, corner_radius=8, height=30)
        row.pack_propagate(False)

        dot = ctk.CTkLabel(
            row,
            text="●",
            width=12,
            font=T.font(T.SIZE_TINY),
            text_color=T.SUCCESS if enabled else T.TEXT_FAINT,
        )
        dot.pack(side="left", padx=(11, 7), pady=8)

        title = ctk.CTkLabel(
            row,
            text=desc,
            font=T.font(T.SIZE_BODY),
            text_color=T.TEXT if enabled else T.TEXT_FAINT,
            anchor="w",
        )
        title.pack(side="left")

        # 按钮先 pack（占最右侧），key 再靠里——空间不够时先牺牲 key，不挤任务名
        pill = self._task_pill(row, enabled, lambda k=key, d=desc: self._toggle_task(k, d))
        pill.pack(side="right", padx=(4, 10), pady=5)

        key_label = None
        if key != desc:
            key_label = ctk.CTkLabel(
                row,
                text=key,
                font=T.mono(T.SIZE_TINY),
                text_color=T.TEXT_FAINT,
                anchor="e",
            )
            key_label.pack(side="right", padx=(6, 2))

        # 整行都能点：按钮小不好瞄，任务名也是常见点击目标
        for widget in (row, dot, title, key_label):
            if widget is None:
                continue
            widget.bind("<Button-1>", lambda _event, k=key, d=desc: self._toggle_task(k, d))

        self._task_rows[key] = {"enabled": bool(enabled), "dot": dot, "title": title, "pill": pill}
        return row

    def _task_pill(self, parent, enabled: bool, command) -> ctk.CTkButton:
        """「启用 / 停止」小胶囊：样式跟旧版 Tag 一致，但可点。"""
        return ctk.CTkButton(
            parent,
            text="启用" if enabled else "停止",
            command=command,
            width=44,
            height=20,
            corner_radius=7,
            border_width=0,
            fg_color=T.SUCCESS_SOFT if enabled else T.NEUTRAL_SOFT,
            hover_color=PILL_HOVER_ON if enabled else T.SURFACE_HOVER,
            text_color=T.SUCCESS if enabled else T.TEXT_FAINT,
            font=T.font(T.SIZE_TINY),
            state="disabled" if self.manager.is_running else "normal",
        )

    # -------------------------------------------------------- 任务开关（写回 JSON）
    def _toggle_task(self, key: str, desc: str):
        """点按钮 / 任务名：翻转这一个任务，写盘成功才改界面。"""
        row = self._task_rows.get(key)
        if not row:
            return
        enabled = not row["enabled"]
        if not self.app.set_plan_task(key, enabled):
            # 写盘失败（执行中 / 文件语法错 / 只读）：界面一律不动，免得显示与文件不符
            return
        self._apply_task_row(key, enabled)
        self._refresh_task_counts()
        self.append(f"{desc or key} 已{'启用' if enabled else '停止'}")

    def _apply_task_row(self, key: str, enabled: bool):
        """只改这一行的颜色和按钮文字，不重建卡片（重建会闪一下）。"""
        row = self._task_rows.get(key)
        if not row:
            return
        row["enabled"] = bool(enabled)
        row["dot"].configure(text_color=T.SUCCESS if enabled else T.TEXT_FAINT)
        row["title"].configure(text_color=T.TEXT if enabled else T.TEXT_FAINT)
        row["pill"].configure(
            text="启用" if enabled else "停止",
            fg_color=T.SUCCESS_SOFT if enabled else T.NEUTRAL_SOFT,
            hover_color=PILL_HOVER_ON if enabled else T.SURFACE_HOVER,
            text_color=T.SUCCESS if enabled else T.TEXT_FAINT,
        )

    def _on_bulk_tasks(self, enabled: bool):
        """全部启用 / 全部停止：一次读一次写，逐个就地刷新，不重建卡片。"""
        keys = list(self._task_rows)
        if not keys:
            return
        if not self.app.set_plan_tasks({key: enabled for key in keys}):
            return
        for key in keys:
            self._apply_task_row(key, enabled)
        self._refresh_task_counts()
        self.append(f"{len(keys)} 个任务 已{'启用' if enabled else '停止'}")

    def _refresh_task_counts(self):
        """开关切完刷新「启用任务 / 总数」和底部提示，口径与 :meth:`refresh` 一致。"""
        enabled = self.app.plan.enabled_keys(self.app.taskset)
        total = sum(
            1 for task in self.app.taskset.tasks.values() if not getattr(task, "watch_only", False)
        )
        self.tile_tasks.set(f"{len(enabled)}/{total}")
        self._refresh_run_hint(enabled)

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
    def _render_templates(self):
        """模板缺图的提示（挪到任务卡底部一行，省一块卡片的高度）。"""
        for widget in self._template_footer.winfo_children():
            widget.destroy()

        missing = self.app.taskset.missing_templates
        if not missing:
            ctk.CTkLabel(
                self._template_footer,
                text="✔  模板图齐全，可以放心跑",
                font=T.font(T.SIZE_TINY),
                text_color=T.SUCCESS,
                anchor="w",
            ).pack(anchor="w")
            return

        first = missing[0]
        ctk.CTkLabel(
            self._template_footer,
            text=f"⚠  缺失 {len(missing)} 张模板图，如 {first}"
            + (" 等" if len(missing) > 1 else "")
            + "，用任务添加器补拍",
            font=T.font(T.SIZE_TINY),
            text_color=T.WARN,
            anchor="w",
            justify="left",
            wraplength=520,
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
        ).grid(row=0, column=0, sticky="w", pady=3)
        self.flow_menu = T.option_menu(
            form, ["—"], self._flow_var, width=140, command=self._on_flow_change
        )
        self.flow_menu.grid(row=0, column=1, sticky="w", pady=3)

        ctk.CTkLabel(
            form, text="兑换码", anchor="w", font=T.font(T.SIZE_BODY), text_color=T.TEXT_MUTED
        ).grid(row=1, column=0, sticky="w", pady=3)
        self.redeem_entry = T.entry(form, placeholder_text="留空则不兑换")
        self.redeem_entry.grid(row=1, column=1, sticky="ew", pady=3)

        self.dry_run_var = ctk.BooleanVar(value=bool(self.app.config.get("dry_run", False)))
        T.switch(form, "仅演练（不真实点击）", self.dry_run_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 2)
        )

    # ------------------------------------------------------------ 循环监控卡
    def _build_watch_card(self, parent):
        card = Card(parent, "循环监控", "主流程空闲时才巡检")
        card.pack(fill="x", pady=(T.GAP_MD, 0))
        self.watch_card = card
        T.text_button(card.actions, "立即检测", self._on_watch_check).pack(side="right")

        self._watch_rows: Dict[str, dict] = {}
        self._watch_state: Dict[str, dict] = {}
        self._updating_watch = False
        self._watch_enabled_var = ctk.BooleanVar(
            value=bool((self.app.config.get("watchers") or {}).get("enabled"))
        )

        head = ctk.CTkFrame(card.body, fg_color="transparent")
        head.pack(fill="x", pady=(0, 2))
        T.switch(
            head,
            "启用巡检",
            self._watch_enabled_var,
            command=self._on_watch_toggle,
        ).pack(side="left")
        self.watch_status = ctk.CTkLabel(
            head, text="未启用", font=T.font(T.SIZE_TINY), text_color=T.TEXT_FAINT, anchor="e"
        )
        self.watch_status.pack(side="right")

        self._watch_body = ctk.CTkFrame(card.body, fg_color="transparent")
        self._watch_body.pack(fill="x", pady=(T.GAP_XS, 0))
        self._watch_hint = ctk.CTkLabel(
            card.body,
            text="",
            font=T.font(T.SIZE_TINY),
            text_color=T.WARN,
            anchor="w",
            justify="left",
            wraplength=340,
        )

    def update_watch(self, state: dict):
        """把监控服务的快照刷到卡片上（App 每 0.5 秒调一次）。"""
        if not hasattr(self, "watch_card"):
            return
        self._watch_state = state or {}
        running = bool(self._watch_state.get("running"))
        message = str(self._watch_state.get("message") or "")
        enabled = bool((self.app.config.get("watchers") or {}).get("enabled"))

        # 状态标签只放短词，长原因（缺模板等）放到下面那行提示里
        if running:
            status_text, status_color = "巡检中", T.SUCCESS
        elif not enabled:
            status_text, status_color = "未启用", T.TEXT_FAINT
        else:
            status_text, status_color = "未启动", T.WARN
        self.watch_status.configure(text=status_text, text_color=status_color)
        if enabled != self._watch_enabled_var.get():
            self._updating_watch = True
            self._watch_enabled_var.set(enabled)
            self._updating_watch = False

        rules = list(self._watch_state.get("rules") or [])
        if [rule.get("key") for rule in rules] != list(self._watch_rows):
            self._rebuild_watch_rows(rules)
        else:
            self._updating_watch = True
            for rule in rules:
                row = self._watch_rows.get(rule.get("key"))
                if not row:
                    continue
                row["var"].set(bool(rule.get("enabled")))
                row["info"].configure(text=f"{rule.get('interval')} · 已领 {rule.get('hits', 0)}")
                note = str(rule.get("note") or "")
                row["note"].configure(
                    text=note,
                    text_color=T.SUCCESS if note == "已领取" else T.TEXT_FAINT,
                )
            self._updating_watch = False

        hints: List[str] = []
        if enabled and not running and message and message not in ("未启动", "已停止", "总开关已关闭"):
            hints.append(message)
        problems = list(self._watch_state.get("problems") or [])
        if problems:
            hints.append("规则有问题：" + "；".join(problems[:2]))
        if hints:
            self._watch_hint.configure(text="\n".join(hints))
            self._watch_hint.pack(fill="x", pady=(4, 0))
        else:
            self._watch_hint.pack_forget()

    def _rebuild_watch_rows(self, rules: List[dict]):
        for widget in self._watch_body.winfo_children():
            widget.destroy()
        self._watch_rows = {}
        self._updating_watch = True

        if not rules:
            ctk.CTkLabel(
                self._watch_body,
                text="还没有巡检规则，可在 config.json → watchers.rules 里添加",
                font=T.font(T.SIZE_TINY),
                text_color=T.TEXT_FAINT,
                anchor="w",
                justify="left",
                wraplength=340,
            ).pack(anchor="w")
            self._updating_watch = False
            return

        for rule in rules:
            row = ctk.CTkFrame(self._watch_body, fg_color=T.SURFACE_ALT, corner_radius=8, height=32)
            row.pack_propagate(False)
            row.pack(fill="x", pady=2)

            var = ctk.BooleanVar(value=bool(rule.get("enabled")))
            T.switch(
                row,
                "",
                var,
                width=36,
                command=lambda key=rule.get("key"): self._on_watch_rule(key),
            ).pack(side="left", padx=(8, 4), pady=4)
            ctk.CTkLabel(
                row,
                text=str(rule.get("name") or rule.get("key")),
                font=T.font(T.SIZE_SMALL),
                text_color=T.TEXT,
                anchor="w",
            ).pack(side="left")
            note = ctk.CTkLabel(
                row,
                text=str(rule.get("note") or ""),
                font=T.font(T.SIZE_TINY),
                text_color=T.TEXT_FAINT,
                anchor="e",
            )
            note.pack(side="right", padx=(4, 10))
            info = ctk.CTkLabel(
                row,
                text=f"{rule.get('interval')} · 已领 {rule.get('hits', 0)}",
                font=T.font(T.SIZE_TINY),
                text_color=T.TEXT_MUTED,
                anchor="e",
            )
            info.pack(side="right", padx=(0, 10))
            self._watch_rows[rule.get("key")] = {"var": var, "note": note, "info": info}

        self._updating_watch = False

    def _on_watch_toggle(self):
        if self._updating_watch:
            return
        self.app.set_watchers_enabled(bool(self._watch_enabled_var.get()))

    def _on_watch_rule(self, key: str):
        if self._updating_watch or not key:
            return
        row = self._watch_rows.get(key)
        if row:
            self.app.set_watch_rule(key, bool(row["var"].get()))

    def _on_watch_check(self):
        self.app.check_watch_now()

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
            height=LOG_HEIGHT,
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
        # watch_only 的任务（红包这类）只由监控调用，不算进「任务开关」的总数
        total = sum(1 for task in taskset.tasks.values() if not getattr(task, "watch_only", False))
        self.tile_tasks.set(f"{len(enabled)}/{total}")

        self._refresh_flow_menu(
            taskset,
            plan.flow or self.app.config.get("flow") or "",
        )
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

    def _refresh_flow_menu(self, taskset, preferred_key: str):
        """菜单显示中文名，值仍是流程 key；两个流程重名时补上 key 区分。"""
        self._flow_keys = {}
        labels: List[str] = []
        for key in taskset.flow_names():
            label = taskset.flow_label(key)
            if label in self._flow_keys:
                label = f"{label}（{key}）"
            self._flow_keys[label] = key
            labels.append(label)

        self.flow_menu.configure(values=labels or ["—"])

        # 优先保留用户当前选择；选择为空/失效时才回落到配置里的流程
        target = self.current_flow()
        if target not in taskset.flows:
            target = preferred_key if preferred_key in taskset.flows else ""
        if target not in taskset.flows:
            names = taskset.flow_names()
            target = names[0] if names else ""
        label = next((name for name, key in self._flow_keys.items() if key == target), "")
        self._flow_var.set(label or target or "—")

    def current_flow(self) -> str:
        """当前下拉项对应的流程 key；取不到就返回空串，让上层回落。"""
        label = self._flow_var.get()
        key = self._flow_keys.get(label)
        if key:
            return key
        # 兼容「变量里直接放 key」的情况（例如配置文件写的就是 key）
        return label if label in self._flow_keys.values() else ""

    def _on_flow_change(self, _value=None):
        self.tile_flow.set(self._flow_var.get() or "—")
        self._refresh_run_hint()

    def _refresh_run_hint(self, enabled: Optional[List[str]] = None):
        if enabled is None:
            enabled = self.app.plan.enabled_keys(self.app.taskset)
        flow = self._flow_var.get()
        self.run_hint.configure(text=f"流程 {flow or '—'} · 将执行 {len(enabled)} 个任务")

    # ------------------------------------------------------------------ 配置
    def apply_to_config(self):
        """把「这一次运行」的选项写回内存配置（落盘由 App 负责）。"""
        flow = self.current_flow()
        if flow:
            self.app.config["flow"] = flow
        self.app.config["dry_run"] = bool(self.dry_run_var.get())
        variables = dict(self.app.config.get("variables") or {})
        variables["redeem_code"] = self.redeem_entry.get().strip()
        self.app.config["variables"] = variables

    def build_run_overrides(self) -> dict:
        return {
            "flow": self.current_flow(),
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
        # 跑起来后这一趟的任务清单已经是快照，按钮先灰掉，免得点了只换来一句拒绝
        switch_state = "disabled" if running else "normal"
        for row in self._task_rows.values():
            try:
                row["pill"].configure(state=switch_state)
            except Exception:
                pass
        for button in self._bulk_buttons:
            try:
                button.configure(state=switch_state)
            except Exception:
                pass
        if not running:
            self._refresh_run_hint()

    def _on_destroy(self, _event=None):
        self._alive = False
