import customtkinter as ctk
from core.task_manager import TaskManager


class BasePage(ctk.CTkFrame):
    def __init__(self, master, task_manager: TaskManager, title: str):
        super().__init__(master)
        self.task_manager = task_manager
        self.task_manager.set_status_callback(self._on_status_change)

        title_label = ctk.CTkLabel(
            self, text=title, font=ctk.CTkFont(size=18, weight="bold")
        )
        title_label.pack(anchor="w", pady=(10, 15), padx=15)

        self._build_settings()

        ctk.CTkLabel(
            self, text="自动任务", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(anchor="w", padx=15, pady=(10, 5))

        self._build_task_list()

        self._build_controls()

        self.log_box = ctk.CTkTextbox(self, height=160)
        self.log_box.pack(fill="x", padx=15, pady=(10, 15))

        self._update_ui_state()

    def _build_settings(self):
        raise NotImplementedError

    def _build_task_list(self):
        raise NotImplementedError

    def _build_controls(self):
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=(10, 5), padx=15, fill="x")

        self.start_btn = ctk.CTkButton(
            btn_frame, text="▶ 开始运行", command=self._on_start, width=120
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ctk.CTkButton(
            btn_frame,
            text="⏹ 停止",
            command=self._on_stop,
            width=100,
            state="disabled",
            fg_color="#D9534F",
            hover_color="#C9302C",
        )
        self.stop_btn.pack(side="left")

    def _on_start(self):
        if self.task_manager.is_running:
            return
        self.log(">>> 任务开始执行...")
        self.task_manager.start(self.mode_name, {})

    def _on_stop(self):
        if not self.task_manager.is_running:
            return
        self.log(">>> 正在停止任务...")
        self.task_manager.stop()

    def _on_status_change(self, status):
        self._update_ui_state()

    def _update_ui_state(self):
        running = self.task_manager.is_running
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def log(self, message: str):
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")


class ADBPage(BasePage):
    mode_name = "adb"

    def __init__(self, master, task_manager: TaskManager):
        super().__init__(master, task_manager, "📱 方案一：Android ADB（模拟器）")

    def _build_settings(self):
        settings_frame = ctk.CTkFrame(self)
        settings_frame.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(settings_frame, text="ADB 配置", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(8, 5), padx=10)

        ctk.CTkLabel(settings_frame, text="ADB 路径:").grid(row=1, column=0, sticky="w", padx=10, pady=3)
        self.adb_path = ctk.CTkEntry(settings_frame, placeholder_text="adb (系统 PATH) 或 完整路径")
        self.adb_path.grid(row=1, column=1, sticky="ew", padx=10, pady=3)

        ctk.CTkLabel(settings_frame, text="设备地址:").grid(row=2, column=0, sticky="w", padx=10, pady=3)
        self.device_addr = ctk.CTkEntry(settings_frame, placeholder_text="127.0.0.1:7555 (MuMu)")
        self.device_addr.grid(row=2, column=1, sticky="ew", padx=10, pady=3)

        ctk.CTkLabel(settings_frame, text="分辨率:").grid(row=3, column=0, sticky="w", padx=10, pady=3)
        res_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        res_frame.grid(row=3, column=1, sticky="w", padx=10, pady=3)
        self.res_w = ctk.CTkEntry(res_frame, width=70, placeholder_text="宽")
        self.res_w.pack(side="left")
        ctk.CTkLabel(res_frame, text=" × ").pack(side="left", padx=5)
        self.res_h = ctk.CTkEntry(res_frame, width=70, placeholder_text="高")
        self.res_h.pack(side="left")
        ctk.CTkLabel(res_frame, text="（留空则自动获取）").pack(side="left", padx=8)

        settings_frame.grid_columnconfigure(1, weight=1)

    def _build_task_list(self):
        tasks_frame = ctk.CTkFrame(self)
        tasks_frame.pack(fill="x", padx=15, pady=(0, 10))

        self.adb_tasks = {}
        tasks = [
            ("main_quest", "主线任务推进"),
            ("gov_affairs", "政务处理"),
            ("building", "建筑升级"),
            ("porridge", "施粥活动"),
            ("school", "县学传业"),
            ("recruit", "招贤任务"),
            ("patrol", "巡视任务"),
            ("tax", "税收收取"),
            ("bandit", "不良司缉盗"),
            ("arena", "以武会友"),
            ("reward", "领取奖励"),
        ]
        for i, (key, label) in enumerate(tasks):
            var = ctk.BooleanVar(value=True)
            cb = ctk.CTkCheckBox(tasks_frame, text=label, variable=var)
            cb.grid(row=i // 2, column=i % 2, sticky="w", padx=10, pady=3)
            self.adb_tasks[key] = var


class ScreenCapturePage(BasePage):
    mode_name = "screen_capture"

    def __init__(self, master, task_manager: TaskManager):
        super().__init__(master, task_manager, "🖥️ 方案二：屏幕截取 + 模拟键鼠（微信小程序）")

    def _build_settings(self):
        settings_frame = ctk.CTkFrame(self)
        settings_frame.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(settings_frame, text="窗口配置", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(8, 5), padx=10)

        ctk.CTkLabel(settings_frame, text="窗口标题:").grid(row=1, column=0, sticky="w", padx=10, pady=3)
        self.window_title = ctk.CTkEntry(settings_frame, placeholder_text="这城有良田 或 WeChat")
        self.window_title.grid(row=1, column=1, sticky="ew", padx=10, pady=3)

        ctk.CTkLabel(settings_frame, text="采集模式:").grid(row=2, column=0, sticky="w", padx=10, pady=3)
        self.capture_mode = ctk.CTkOptionMenu(
            settings_frame, values=["mss（快速）", "PIL（兼容）"]
        )
        self.capture_mode.grid(row=2, column=1, sticky="w", padx=10, pady=3)

        ctk.CTkLabel(settings_frame, text="操作间隔:").grid(row=3, column=0, sticky="w", padx=10, pady=3)
        interval_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        interval_frame.grid(row=3, column=1, sticky="w", padx=10, pady=3)
        self.interval = ctk.CTkEntry(interval_frame, width=70, placeholder_text="1.0")
        self.interval.pack(side="left")
        ctk.CTkLabel(interval_frame, text="秒").pack(side="left", padx=5)

        settings_frame.grid_columnconfigure(1, weight=1)

    def _build_task_list(self):
        tasks_frame = ctk.CTkFrame(self)
        tasks_frame.pack(fill="x", padx=15, pady=(0, 10))

        self.sc_tasks = {}
        tasks = [
            ("main_quest", "主线任务推进"),
            ("gov_affairs", "政务处理"),
            ("building", "建筑升级"),
            ("porridge", "施粥活动"),
            ("school", "县学传业"),
            ("recruit", "招贤任务"),
            ("patrol", "巡视任务"),
            ("tax", "税收收取"),
            ("bandit", "不良司缉盗"),
            ("arena", "以武会友"),
            ("reward", "领取奖励"),
        ]
        for i, (key, label) in enumerate(tasks):
            var = ctk.BooleanVar(value=True)
            cb = ctk.CTkCheckBox(tasks_frame, text=label, variable=var)
            cb.grid(row=i // 2, column=i % 2, sticky="w", padx=10, pady=3)
            self.sc_tasks[key] = var
