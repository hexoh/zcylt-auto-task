"""主窗口外壳：顶栏（品牌 / 方案切换 / 状态 / 设置）+ 页面容器。

结构：
    顶栏      品牌、方案切换器、运行状态、设置入口、更多菜单
    首页      ui/home.py     任务计划 + 运行选项 + 开始/停止 + 日志
    设置页    ui/settings.py ADB / 屏幕截取两个 Tab，参数与测试都在里面

没有左侧导航栏——页面只有两块，用顶栏的按钮来回切，能少一列视觉噪音。
哪些入口出现、哪些能点，全部由 config.json 的 features 段决定（core/features.py）。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog
from typing import List, Optional, Tuple

import customtkinter as ctk

from core.devtools import dev_mode_source, resolve_dev_mode
from core.features import (
    disabled_reason,
    feature,
    is_enabled,
    is_visible,
    modes,
    resolve_mode,
)
from core.settings import load_config, save_config
from core.task_manager import TaskManager, TaskStatus
from core.task_plan import (
    DEFAULT_PLAN_FILE,
    TaskPlan,
    build_plan_document,
    ensure_plan,
    load_plan,
    save_plan,
)
from core.task_schema import TaskSet, load_tasks
from ui import theme as T
from ui.home import HomePage
from ui.settings import SettingsPage
from ui.widgets import ModeSwitcher

APP_TITLE = "这城有良田 · 自动任务脚本"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.config_path = os.path.join(self.root_dir, "config.json")
        self.config = load_config(self.config_path)
        # 任务添加器只给配置作者用，发布版隐藏（详见 core/devtools.py）
        self.dev_mode = resolve_dev_mode(self.config)
        self.startup_error = ""

        # 计划文件先于任务定义加载：它可能自己指定 tasks_path
        self._plan_path = self._abs_path(self.config.get("plan_path") or DEFAULT_PLAN_FILE)
        self.plan: TaskPlan = load_plan(self._plan_path)
        self.taskset: TaskSet = self._load_taskset()
        if not self.startup_error and not self.plan.exists:
            self.plan = ensure_plan(
                self._plan_path,
                self.taskset,
                flow=self.config.get("flow", ""),
                tasks_path=self._tasks_path_raw(),
            )
        self.active_mode = resolve_mode(self.config, self.config.get("mode"))

        self.manager = TaskManager()
        # 回调来自后台线程：先入队，回到主线程再改界面
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self.manager.set_callbacks(
            on_status=lambda status: self._events.put(("status", status)),
            on_log=lambda line: self._events.put(("log", line)),
            on_finish=lambda report: self._events.put(("finish", report)),
        )

        self.pages: dict = {}
        self._page_name = ""
        self._alive = True
        self._tools_menu = None
        self._tools_bind = None

        ctk.set_appearance_mode("system")
        self.title(APP_TITLE)
        self.geometry("1080x800")
        self.minsize(980, 700)
        self.configure(fg_color=T.BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_topbar()
        ctk.CTkFrame(self, height=1, fg_color=T.BORDER, corner_radius=0).pack(fill="x")
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        self.after(40, self.show_home)
        self._schedule(100, self._drain_events)
        self._schedule(1500, self._watch_plan)

    # ------------------------------------------------------------------ 路径
    def _abs_path(self, path: str) -> str:
        path = str(path or "").strip()
        if not path:
            return self.root_dir
        return path if os.path.isabs(path) else os.path.join(self.root_dir, path)

    def _display_path(self, path: str) -> str:
        """项目内的文件存相对路径，换机器/换目录也能用。"""
        try:
            relative = os.path.relpath(path, self.root_dir)
        except ValueError:
            return path
        return path if relative.startswith("..") else relative

    def _tasks_path_raw(self) -> str:
        return (self.plan.tasks_path or self.config.get("tasks_path") or "tasks").strip()

    def _tasks_path(self) -> str:
        return self._abs_path(self._tasks_path_raw())

    def _load_taskset(self) -> TaskSet:
        try:
            return load_tasks(self._tasks_path(), base_dir=self.root_dir)
        except Exception as exc:
            self.startup_error = str(exc)
            return TaskSet(base_dir=self.root_dir, source=self._tasks_path())

    def _reload_taskset(self):
        try:
            self.taskset = load_tasks(self._tasks_path(), base_dir=self.root_dir)
            self.startup_error = ""
        except Exception as exc:
            self.startup_error = str(exc)

    # ------------------------------------------------------------------ 顶栏
    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0, height=68)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=T.PAGE_PAD, pady=10)

        brand = ctk.CTkFrame(inner, fg_color="transparent")
        brand.pack(side="left")
        ctk.CTkLabel(brand, text="🏯", font=T.font(21), width=28).pack(side="left", padx=(0, T.GAP_SM))
        titles = ctk.CTkFrame(brand, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(
            titles, text="这城有良田", font=T.font(16, "bold"), text_color=T.TEXT, anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="自动任务 · 本地运行",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")

        self.switcher: Optional[ModeSwitcher] = None
        items = modes(self.config)
        if items:
            self.switcher = ModeSwitcher(
                inner,
                items,
                command=self.switch_mode,
                disabled_command=self._on_mode_blocked,
            )
            self.switcher.pack(side="left", padx=(T.GAP_XL + 4, 0))
            self.switcher.set_active(self.active_mode)
        else:
            ctk.CTkLabel(
                inner,
                text="未启用任何运行方案（config.json → features.modes）",
                font=T.font(T.SIZE_SMALL),
                text_color=T.DANGER,
            ).pack(side="left", padx=(T.GAP_XL + 4, 0))

        self.settings_btn: Optional[ctk.CTkButton] = None
        if is_visible(self.config, "settings"):
            self.settings_btn = T.ghost(
                inner,
                "⚙  设置",
                self._toggle_settings,
                width=104,
                height=34,
                state="normal" if is_enabled(self.config, "settings") else "disabled",
            )
            self.settings_btn.pack(side="right")

        chip = ctk.CTkFrame(inner, fg_color=T.SURFACE_ALT, corner_radius=8)
        chip.pack(side="right", padx=(0, T.GAP_SM))
        self.status_label = ctk.CTkLabel(
            chip, text="● 空闲", font=T.font(T.SIZE_SMALL), text_color=T.TEXT_MUTED
        )
        self.status_label.pack(padx=10, pady=5)

        if self._tool_items():
            self.more_btn = T.ghost(inner, "⋯", self._open_tools_menu, width=38, height=34)
            self.more_btn.pack(side="right", padx=(0, T.GAP_SM))
        else:
            self.more_btn = None

    # ------------------------------------------------------------------ 页面
    def _page(self, name: str):
        page = self.pages.get(name)
        if page is not None:
            return page
        page = HomePage(self.container, self) if name == "home" else SettingsPage(self.container, self)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[name] = page
        return page

    def show_home(self):
        page = self._page("home")
        page.tkraise()
        self._page_name = "home"
        page.refresh()
        if self.settings_btn is not None:
            self.settings_btn.configure(text="⚙  设置", command=self._toggle_settings)
        self._refresh_status(self.manager.status)

    def show_settings(self):
        if not is_enabled(self.config, "settings"):
            self.error("设置已被关闭", disabled_reason(self.config, "settings"))
            return
        page = self._page("settings")
        page.tkraise()
        self._page_name = "settings"
        page.sync_from_config()
        if self.settings_btn is not None:
            self.settings_btn.configure(text="←  返回首页", command=self._toggle_settings)

    def _toggle_settings(self):
        if self._page_name == "settings":
            self.show_home()
        else:
            self.show_settings()

    # ------------------------------------------------------------------ 方案
    def switch_mode(self, mode: str):
        if mode == self.active_mode:
            return
        if self.manager.is_running:
            self.error("任务执行中", "请先停止当前任务再切换方案")
            if self.switcher is not None:
                self.switcher.set_active(self.active_mode)
            return
        if not is_enabled(self.config, "modes", mode):
            self._on_mode_blocked(mode)
            return
        self.active_mode = mode
        self.config["mode"] = mode
        if self.switcher is not None:
            self.switcher.set_active(mode)
        home = self.pages.get("home")
        if home is not None:
            home.refresh()
        self.persist_config()

    def _on_mode_blocked(self, mode: str):
        self.error("方案未启用", disabled_reason(self.config, "modes", mode))

    def mode_label(self) -> str:
        if not self.active_mode:
            return "未启用"
        return feature(self.config, "modes", self.active_mode)["label"]

    def _backend_config(self, mode: str) -> dict:
        settings = self.pages.get("settings")
        if settings is not None:
            return settings.collect_backend(mode)
        return dict(self.config.get(mode) or {})

    # ------------------------------------------------------------------ 运行
    def build_run_config(self) -> Optional[dict]:
        mode = self.active_mode
        if not mode:
            self.error("没有可用方案", "config.json 的 features.modes 里没有启用的运行方案。")
            return None
        if not is_enabled(self.config, "modes", mode):
            self._on_mode_blocked(mode)
            return None

        enabled = self.plan.enabled_keys(self.taskset)
        if not enabled:
            self.error(
                "没有启用的任务",
                "在 task_plan.json 的 tasks 里把要跑的任务改成 true，"
                "存盘后界面会自动重新载入（也可点「重新载入」）。",
            )
            return None

        home = self.pages.get("home")
        overrides = (
            home.build_run_overrides()
            if home is not None
            else {"flow": self.config.get("flow", ""), "dry_run": False, "variables": {}}
        )
        variables = dict(self.config.get("variables") or {})
        variables.update(self.plan.variables)
        variables.update(overrides.get("variables") or {})

        flow = overrides.get("flow") or self.plan.flow or self.config.get("flow") or ""
        return {
            "root": self.root_dir,
            "tasks_path": self._tasks_path(),
            "mode": "mock" if overrides.get("dry_run") else mode,
            "backend": self._backend_config(mode),
            "flow": flow,
            "flows": [flow] if flow else [],
            "task_keys": enabled,
            "selection": self.plan.selection(),
            "variables": variables,
            "notify": self.config.get("notify") or {},
            "dry_run": bool(overrides.get("dry_run")),
            "screenshot_on_fail": True,
            "repeat": max(1, int(self.config.get("repeat", 1) or 1)),
        }

    # ------------------------------------------------------------------ 计划
    def reload_plan(self):
        if self.manager.is_running:
            self.error("任务执行中", "请先停止任务再重新载入配置")
            return
        self.plan = load_plan(self._plan_path)
        self._reload_taskset()
        home = self.pages.get("home")
        if home is not None:
            home.refresh()
            if self.plan.error:
                home.append(f"计划文件有问题：{self.plan.error}")
            else:
                home.append("已重新载入任务计划")
        self.persist_config()

    def import_plan(self):
        if self.manager.is_running:
            self.error("任务执行中", "请先停止任务再导入计划")
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="导入任务计划 JSON",
            initialdir=self.root_dir,
            filetypes=[("JSON 文件", "*.json"), ("全部文件", "*.*")],
        )
        if not path:
            return
        plan = load_plan(path)
        if plan.error:
            self.error("计划文件无法使用", plan.error)
            return
        self._plan_path = os.path.abspath(path)
        self.config["plan_path"] = self._display_path(self._plan_path)
        self.plan = plan
        self._reload_taskset()
        home = self._page("home")
        home.refresh()
        home.append(f"已导入计划：{os.path.basename(path)}")
        self.persist_config()

    def open_plan_file(self):
        if not os.path.isfile(self._plan_path):
            save_plan(
                build_plan_document(
                    self.taskset,
                    flow=self.config.get("flow", ""),
                    tasks_path=self._tasks_path_raw(),
                ),
                self._plan_path,
            )
            self.plan = load_plan(self._plan_path)
            home = self.pages.get("home")
            if home is not None:
                home.refresh()
        self._open_path(self._plan_path)

    def open_config_file(self):
        self._open_path(self.config_path)

    def _watch_plan(self):
        """计划文件被外部编辑器改了就自动重载——省掉「改完还要点一下」的来回。"""
        if not self._alive:
            return
        try:
            mtime = os.path.getmtime(self._plan_path) if os.path.isfile(self._plan_path) else 0.0
        except OSError:
            mtime = 0.0
        changed = bool(mtime) and mtime != (self.plan.mtime or 0.0)
        if changed and not self.manager.is_running:
            self.plan = load_plan(self._plan_path)
            self._reload_taskset()
            home = self.pages.get("home")
            if home is not None:
                home.refresh()
                if not self.plan.error:
                    home.append("检测到 task_plan.json 有改动，已自动重新载入")
        self._schedule(1500, self._watch_plan)

    def _schedule(self, delay_ms: int, callback):
        """窗口销毁后别再往 Tk 里塞 after，否则退出时会报 invalid command name。"""
        try:
            self.after(delay_ms, callback)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 工具
    def _tool_items(self) -> List[Tuple[str, str, object, bool]]:
        """更多菜单里的条目：(key, 文案, 回调, 是否可点)。"""
        handlers = {
            "reload": self.reload_plan,
            "templates": lambda: self.open_dir(os.path.join(self.root_dir, "templates")),
            "logs": lambda: self.open_dir(os.path.join(self.root_dir, "logs")),
            "adder": self._open_adder,
        }
        items: List[Tuple[str, str, object, bool]] = []
        for key, handler in handlers.items():
            if not is_visible(self.config, "tools", key):
                continue
            enabled = is_enabled(self.config, "tools", key)
            if key == "adder" and not self.dev_mode:
                enabled = False
            items.append((key, feature(self.config, "tools", key)["label"], handler, enabled))
        return items

    def _open_tools_menu(self):
        """顶栏「⋯」下拉：用应用内面板而不是系统菜单，视觉才和整套界面一致。"""
        if self.more_btn is None:
            return
        if self._tools_menu is not None:
            self._close_tools_menu()
            return

        items = self._tool_items()
        panel = ctk.CTkFrame(
            self,
            fg_color=T.SURFACE,
            corner_radius=10,
            border_width=1,
            border_color=T.BORDER,
            width=232,
        )
        ctk.CTkFrame(panel, height=0, fg_color="transparent").pack()
        for _key, label, handler, enabled in items:
            ctk.CTkButton(
                panel,
                text=label,
                anchor="w",
                height=30,
                corner_radius=7,
                fg_color="transparent",
                hover_color=T.SURFACE_HOVER if enabled else T.SURFACE,
                text_color=T.TEXT if enabled else T.TEXT_FAINT,
                font=T.font(T.SIZE_SMALL),
                border_width=0,
                state="normal" if enabled else "disabled",
                command=(lambda h=handler: self._run_tool(h)) if enabled else None,
            ).pack(fill="x", padx=5, pady=1)
        if items:
            ctk.CTkFrame(panel, height=1, fg_color=T.BORDER, corner_radius=0).pack(
                fill="x", padx=8, pady=3
            )
        ctk.CTkButton(
            panel,
            text="打开 config.json",
            anchor="w",
            height=30,
            corner_radius=7,
            fg_color="transparent",
            hover_color=T.SURFACE_HOVER,
            text_color=T.TEXT,
            font=T.font(T.SIZE_SMALL),
            border_width=0,
            command=lambda: self._run_tool(self.open_config_file),
        ).pack(fill="x", padx=5, pady=(0, 5))

        x = self.more_btn.winfo_rootx() - self.winfo_rootx() - 232 + self.more_btn.winfo_width()
        y = self.more_btn.winfo_rooty() - self.winfo_rooty() + self.more_btn.winfo_height() + 6
        x = max(8, min(x, self.winfo_width() - 240))
        panel.place(x=x, y=y)
        panel.lift()
        self._tools_menu = panel
        # 点窗口别处就收起（点菜单项时命令先跑，再收面板）
        self._tools_bind = self.bind("<Button-1>", lambda _e: self._close_tools_menu(), add="+")
        self.bind("<Escape>", lambda _e: self._close_tools_menu(), add="+")

    def _run_tool(self, handler):
        self._close_tools_menu()
        handler()

    def _close_tools_menu(self):
        panel, self._tools_menu = self._tools_menu, None
        if panel is None:
            return
        if self._tools_bind is not None:
            try:
                self.unbind("<Button-1>", self._tools_bind)
            except tk.TclError:
                pass
            self._tools_bind = None
        try:
            panel.destroy()
        except tk.TclError:
            pass

    def _open_adder(self):
        if not self.dev_mode:
            self.error(
                "开发者工具已隐藏",
                f"{dev_mode_source(self.config)}。\n"
                "任务添加器用于录制 tasks/*.json，普通用户不需要它。\n"
                "需要时设置环境变量 ZCYL_DEV=1 再启动，或在 config.json 里写 \"dev_mode\": true。",
            )
            return
        try:
            from tools.adder import AdderWindow
        except Exception as exc:
            self.error("无法打开任务添加器", f"{exc}\n请先安装依赖：pip install -r requirements.txt")
            return
        mode = self.active_mode or "adb"
        adb_cfg = dict(self._backend_config("adb"))
        screen_cfg = self._backend_config("screen")
        adb_cfg["window_title"] = screen_cfg.get("window_title", "")
        try:
            self._adder = AdderWindow(self, root=self.root_dir, mode=mode, adb_config=adb_cfg)
            self._adder.focus_force()
        except Exception as exc:
            self.error("无法打开任务添加器", str(exc))

    def open_dir(self, path: str):
        os.makedirs(path, exist_ok=True)
        self._open_path(path)

    def _open_path(self, path: str):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self.error("打开失败", str(exc))

    # ------------------------------------------------------------------ 配置
    def persist_config(self) -> bool:
        settings = self.pages.get("settings")
        home = self.pages.get("home")
        if settings is not None:
            settings.apply_to_config()
        if home is not None:
            home.apply_to_config()
        if self.active_mode:
            self.config["mode"] = self.active_mode
        self.config["plan_path"] = self._display_path(self._plan_path)
        return save_config(self.config, self.config_path)

    # ------------------------------------------------------------------ 事件
    def _drain_events(self):
        if not self._alive:
            return
        for _ in range(500):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self._refresh_status(payload)
            elif kind == "log":
                home = self.pages.get("home")
                if home is not None:
                    home.append(payload)
            elif kind == "finish":
                self._on_task_finish(payload)
        self._schedule(100, self._drain_events)

    def _refresh_status(self, status: TaskStatus):
        styles = {
            TaskStatus.IDLE: ("● 空闲", T.TEXT_MUTED),
            TaskStatus.STARTING: ("● 启动中…", T.WARN),
            TaskStatus.RUNNING: ("● 运行中", T.SUCCESS),
            TaskStatus.STOPPING: ("● 停止中…", T.WARN),
        }
        text, color = styles.get(status, styles[TaskStatus.IDLE])
        self.status_label.configure(text=text, text_color=color)
        home = self.pages.get("home")
        if home is not None:
            try:
                home.on_status(status)
            except Exception:
                pass

    def _on_task_finish(self, report):
        if report is None:
            return
        self.title(f"{APP_TITLE} — {report.brief}")

    # ------------------------------------------------------------------ 通用
    def error(self, title: str, message: str):
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.configure(fg_color=T.BG)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.after(60, dialog.grab_set)

        frame = ctk.CTkFrame(
            dialog, fg_color=T.SURFACE, corner_radius=12, border_width=1, border_color=T.BORDER
        )
        frame.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(
            frame, text=title, font=T.font(T.SIZE_TITLE, "bold"), text_color=T.TEXT, anchor="w"
        ).pack(anchor="w", padx=16, pady=(14, 6))
        box = ctk.CTkTextbox(
            frame,
            width=430,
            height=110,
            font=T.font(T.SIZE_SMALL),
            fg_color=T.SURFACE_ALT,
            border_width=0,
            corner_radius=8,
            text_color=T.TEXT,
            wrap="word",
        )
        box.pack(fill="both", expand=True, padx=16)
        box.insert("1.0", message)
        box.configure(state="disabled")
        T.primary(frame, "知道了", dialog.destroy, width=96, height=34).pack(pady=(10, 14))

        dialog.update_idletasks()
        width, height = 500, 300
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 3
        dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _on_close(self):
        self._alive = False
        self.persist_config()
        if self.manager.is_running:
            self.manager.stop()
            self.manager.wait(3)
        self.destroy()
