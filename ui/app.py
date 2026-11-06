"""主窗口：方案切换 + 状态 + 工具入口 + 页面容器。"""

from __future__ import annotations

import os
import queue
import subprocess
import sys

import customtkinter as ctk

from core.devtools import dev_mode_source, resolve_dev_mode
from core.settings import load_config, save_config
from core.task_manager import TaskManager, TaskStatus
from core.task_schema import TaskSet, load_tasks
from ui.pages import ADBPage, BasePage, ScreenCapturePage

APP_TITLE = "这城有良田 · 自动任务脚本"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.config = load_config(os.path.join(self.root_dir, "config.json"))
        # 坐标拾取器/任务构建器只给配置作者用，发布版隐藏（详见 core/devtools.py）
        self.dev_mode = resolve_dev_mode(self.config)
        self.taskset = self._load_taskset()
        self._current_page_name = None
        self._current_page: BasePage | None = None
        self._picker = None

        self.manager = TaskManager()
        # 回调可能来自后台线程：先入队，再由主线程统一消费（Tk 只能在主线程改 UI）
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self.manager.set_callbacks(
            on_status=lambda status: self._events.put(("status", status)),
            on_log=lambda line: self._events.put(("log", line)),
            on_finish=lambda report: self._events.put(("finish", report)),
        )

        self.title(APP_TITLE)
        self.geometry("1060x780")
        self.minsize(920, 680)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.after(80, self._switch_page)
        self.after(100, self._drain_events)

    # ------------------------------------------------------------------ 数据
    def _load_taskset(self) -> TaskSet:
        path = self.config.get("tasks_path") or os.path.join(self.root_dir, "tasks")
        if not os.path.isabs(path):
            path = os.path.join(self.root_dir, path)
        try:
            return load_tasks(path, base_dir=self.root_dir)
        except Exception as exc:
            self._startup_error = str(exc)
            return TaskSet(base_dir=self.root_dir, source=path)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(
            head, text="🏯 " + APP_TITLE, font=ctk.CTkFont(size=22, weight="bold")
        ).pack(side="left")
        ctk.CTkLabel(
            head, text="", text_color="gray"
        ).pack(side="left", padx=10, pady=(6, 0))

        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=18, pady=(0, 16))

        sidebar = ctk.CTkFrame(main, width=200, corner_radius=10)
        sidebar.pack(side="left", fill="y", padx=(10, 5), pady=10)
        sidebar.pack_propagate(False)

        ctk.CTkLabel(
            sidebar, text="⚙️ 方案选择", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(pady=(18, 14))

        self.btn_adb = ctk.CTkButton(
            sidebar,
            text="📱 模拟器 (ADB)",
            command=lambda: self._switch_page("adb"),
            height=42,
            corner_radius=8,
        )
        self.btn_adb.pack(pady=5, padx=12, fill="x")

        self.btn_sc = ctk.CTkButton(
            sidebar,
            text="🖥️ 小程序 (截屏)",
            command=lambda: self._switch_page("screen"),
            height=42,
            corner_radius=8,
        )
        self.btn_sc.pack(pady=5, padx=12, fill="x")

        ctk.CTkLabel(
            sidebar, text="🧰 工具", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(pady=(22, 10))

        tools = []
        if self.dev_mode:
            # 任务添加器：发布版不显示（普通用户只需要一份 tasks/*.json）
            tools.append(("🎯 任务添加器", self._open_adder))
        tools.extend(
            [
                ("🔄 重载配置文件", self.reload_tasks),
                ("📂 模板目录", lambda: self._open_dir(os.path.join(self.root_dir, "templates"))),
                ("🗂 日志目录", lambda: self._open_dir(os.path.join(self.root_dir, "logs"))),
            ]
        )

        for text, command in tools:
            ctk.CTkButton(
                sidebar,
                text=text,
                command=command,
                height=34,
                fg_color="transparent",
                border_width=1,
                text_color=("black", "white"),
                hover_color=("#e5e5e5", "#2f2f2f"),
            ).pack(pady=3, padx=12, fill="x")

        if self.dev_mode:
            ctk.CTkLabel(
                sidebar,
                text="开发者模式（发布版不显示任务添加器）",
                text_color=("gray", "gray"),
                font=ctk.CTkFont(size=10),
                wraplength=170,
            ).pack(side="bottom", pady=(0, 2))

        ctk.CTkLabel(
            sidebar,
            text=f"任务 {len(self.taskset.tasks)} · 模板缺失 {len(self.taskset.missing_templates)}",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(side="bottom", pady=(0, 6))

        self.status_label = ctk.CTkLabel(
            sidebar, text="● 空闲", font=ctk.CTkFont(size=13), text_color="gray"
        )
        self.status_label.pack(side="bottom", pady=(10, 4))

        self.content = ctk.CTkFrame(main, corner_radius=10)
        self.content.pack(side="right", fill="both", expand=True, padx=(5, 10), pady=10)

        if getattr(self, "_startup_error", None):
            self.after(300, lambda: self.error("任务配置有问题", self._startup_error))

    # ------------------------------------------------------------------ 页面
    def _switch_page(self, page_name: str | None = None):
        page_name = page_name or self.config.get("mode", "adb")
        if page_name not in ("adb", "screen"):
            page_name = "adb"
        if self.manager.is_running:
            self.error("任务执行中", "请先停止当前任务再切换方案")
            return
        if page_name == self._current_page_name:
            return

        # 切换方案前把当前页的勾选（含点位选择）落进内存配置，新页面据此回填
        if self._current_page is not None:
            self.persist_config()

        self._current_page_name = page_name
        self.config["mode"] = page_name
        for widget in self.content.winfo_children():
            widget.destroy()

        page = ADBPage(self.content, self) if page_name == "adb" else ScreenCapturePage(self.content, self)
        page.pack(fill="both", expand=True)
        self._current_page = page
        self._highlight_sidebar(page_name)
        self._on_task_status(self.manager.status)

    def _highlight_sidebar(self, active: str):
        active_color = ("#3a7ebf", "#1F6AA5")
        for name, btn in (("adb", self.btn_adb), ("screen", self.btn_sc)):
            if name == active:
                btn.configure(fg_color=active_color, text_color="white", border_width=0)
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=("black", "white"),
                    border_width=1,
                    hover_color=("#e5e5e5", "#2f2f2f"),
                )

    def reload_tasks(self):
        if self.manager.is_running:
            self.error("任务执行中", "请先停止任务再重载配置")
            return
        try:
            path = self.config.get("tasks_path") or os.path.join(self.root_dir, "tasks")
            if not os.path.isabs(path):
                path = os.path.join(self.root_dir, path)
            self.taskset = load_tasks(path, base_dir=self.root_dir)
        except Exception as exc:
            self.error("重载失败", str(exc))
            return

        # 重建当前页，任务列表与流程下拉跟着 JSON 走
        current = self._current_page_name
        self._current_page_name = None
        self._switch_page(current or "adb")
        if self._current_page:
            self._current_page.append(
                f"配置已重载：{len(self.taskset.tasks)} 个任务 / "
                f"{len(self.taskset.flows)} 个流程"
            )

    # ------------------------------------------------------------------ 工具
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
        mode = "adb" if self._current_page_name == "adb" else "screen"
        adb_cfg = dict(self.config.get("adb") or {})
        screen_cfg = self.config.get("screen") or {}
        adb_cfg["window_title"] = screen_cfg.get("window_title", "")
        try:
            self._picker = AdderWindow(self, root=self.root_dir, mode=mode, adb_config=adb_cfg)
            self._picker.focus_force()
        except Exception as exc:
            self.error("无法打开任务添加器", str(exc))

    def _open_dir(self, path: str):
        os.makedirs(path, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self.error("打开目录失败", str(exc))

    # ------------------------------------------------------------------ 配置
    def persist_config(self, page: BasePage | None = None):
        page = page or self._current_page
        if page is not None:
            try:
                if page.mode_name == "adb":
                    self.config["adb"] = page.collect_backend_config()
                else:
                    self.config["screen"] = page.collect_backend_config()
                cfg = page.collect_config()
                self.config["flow"] = cfg["flow"]
                self.config["dry_run"] = cfg["dry_run"]
                self.config["variables"] = cfg["variables"]
                self.config["notify"] = cfg["notify"]
                self.config["selection"] = cfg.get("selection") or {}
            except Exception:
                pass
            self.config["mode"] = page.mode_name
        save_config(self.config, os.path.join(self.root_dir, "config.json"))

    # ------------------------------------------------------------------ 事件
    def _drain_events(self):
        """主线程消费后台事件（状态 / 日志 / 结束）。"""
        for _ in range(500):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self._on_task_status(payload)
            elif kind == "log":
                self._on_task_log(payload)
            elif kind == "finish":
                self._on_task_finish(payload)
        self.after(100, self._drain_events)

    def _on_task_status(self, status: TaskStatus):
        if status == TaskStatus.RUNNING:
            text, color = "● 运行中", "#5cb85c"
        elif status == TaskStatus.STARTING:
            text, color = "● 启动中…", "#f0ad4e"
        elif status == TaskStatus.STOPPING:
            text, color = "● 停止中…", "#f0ad4e"
        else:
            text, color = "● 空闲", "gray"
        self.status_label.configure(text=text, text_color=color)
        page = self._current_page
        if page is not None:
            try:
                page.on_status(status)
            except Exception:
                pass

    def _on_task_log(self, line: str):
        page = self._current_page
        if page is not None:
            try:
                page.append(line)
            except Exception:
                pass

    def _on_task_finish(self, report):
        if report is None:
            return
        # 总结行由执行器通过 Logger.ui 推给界面，这里只更新标题，避免重复一行
        self.title(f"{APP_TITLE} — {report.brief}")

    # ------------------------------------------------------------------ 通用
    def error(self, title: str, message: str):
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("460x240")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.after(50, dialog.grab_set)

        ctk.CTkLabel(
            dialog,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(18, 6))
        box = ctk.CTkTextbox(dialog, width=420, height=120, font=ctk.CTkFont(size=12))
        box.pack(padx=16, pady=6)
        box.insert("1.0", message)
        box.configure(state="disabled")
        ctk.CTkButton(dialog, text="知道了", command=dialog.destroy, width=100).pack(pady=8)

    def _on_close(self):
        self.persist_config()
        if self.manager.is_running:
            self.manager.stop()
            self.manager.wait(3)
        self.destroy()
