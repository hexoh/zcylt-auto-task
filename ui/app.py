import customtkinter as ctk
from core.task_manager import TaskManager, TaskStatus
from ui.pages import ADBPage, ScreenCapturePage


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("这城有良田 - 自动任务脚本")
        self.geometry("920x680")
        self.minsize(800, 600)

        self.task_manager = TaskManager()
        self.task_manager.set_status_callback(self._on_task_status_change)

        self._current_page_name = None

        self._build_ui()

        self.after(100, lambda: self._switch_page("adb"))

    def _build_ui(self):
        title = ctk.CTkLabel(
            self,
            text="🏯 这城有良田 · 自动任务脚本",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title.pack(pady=(18, 12))

        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        sidebar = ctk.CTkFrame(main, width=190, corner_radius=10)
        sidebar.pack(side="left", fill="y", padx=(10, 5), pady=10)
        sidebar.pack_propagate(False)

        ctk.CTkLabel(
            sidebar, text="⚙️ 方案选择", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(pady=(18, 20))

        self.btn_adb = ctk.CTkButton(
            sidebar,
            text="📱 模拟器 (ADB)",
            command=lambda: self._switch_page("adb"),
            height=44,
            corner_radius=8,
        )
        self.btn_adb.pack(pady=6, padx=12, fill="x")

        self.btn_sc = ctk.CTkButton(
            sidebar,
            text="🖥️ 微信小程序 (截屏)",
            command=lambda: self._switch_page("screen_capture"),
            height=44,
            corner_radius=8,
        )
        self.btn_sc.pack(pady=6, padx=12, fill="x")

        self.status_label = ctk.CTkLabel(
            sidebar, text="● 空闲", font=ctk.CTkFont(size=13), text_color="gray"
        )
        self.status_label.pack(side="bottom", pady=18)

        self.content = ctk.CTkFrame(main, corner_radius=10)
        self.content.pack(side="right", fill="both", expand=True, padx=(5, 10), pady=10)

    def _switch_page(self, page_name: str):
        if self.task_manager.is_running:
            self._show_error("任务执行中", "请等待当前任务执行完毕后再切换方案")
            return

        if page_name == self._current_page_name:
            return

        self._current_page_name = page_name

        for w in self.content.winfo_children():
            w.destroy()

        if page_name == "adb":
            page = ADBPage(self.content, self.task_manager)
        else:
            page = ScreenCapturePage(self.content, self.task_manager)

        page.pack(fill="both", expand=True)

        self._highlight_sidebar(page_name)

    def _highlight_sidebar(self, active: str):
        adb_active = active == "adb"
        self.btn_adb.configure(
            fg_color="#1F6AA5" if adb_active else "transparent",
            border_width=0,
            text_color=("white" if adb_active else ("black", "white")),
        )
        self.btn_sc.configure(
            fg_color="#1F6AA5" if not adb_active else "transparent",
            border_width=0,
            text_color=("white" if not adb_active else ("black", "white")),
        )

    def _on_task_status_change(self, status: TaskStatus):
        if status == TaskStatus.RUNNING:
            self.status_label.configure(text="● 运行中", text_color="#5cb85c")
        elif status == TaskStatus.STOPPING:
            self.status_label.configure(text="● 停止中...", text_color="#f0ad4e")
        else:
            self.status_label.configure(text="● 空闲", text_color="gray")

    def _show_error(self, title: str, message: str):
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("340x160")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text=message, wraplength=280, font=ctk.CTkFont(size=13)
        ).pack(pady=(30, 20), padx=20)

        ctk.CTkButton(dialog, text="确定", command=dialog.destroy).pack(pady=10)
