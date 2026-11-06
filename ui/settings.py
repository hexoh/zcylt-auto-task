"""设置页：ADB 与屏幕截取的参数、连接测试都在这里。

以前这些表单分散在两个「方案页」上，切方案等于切整页，改一个路径要先切回去。
现在收敛成一页两个 Tab，参数、测试、保存结果互不打架；首页只负责跑。
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Dict, List, Optional

import customtkinter as ctk

from core.features import feature, is_enabled
from core.task_manager import probe
from ui import theme as T
from ui.widgets import Banner, Card, form_row

CAPTURE_LABELS = {"mss（快速）": "mss", "PIL（兼容）": "pil"}
CAPTURE_KEYS = {value: label for label, value in CAPTURE_LABELS.items()}

TAB_ORDER = ("adb", "screen")


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._ui: Dict[str, dict] = {}
        self._result_queue: "queue.Queue[tuple]" = queue.Queue()
        self._alive = True

        self._build()
        self.bind("<Destroy>", self._on_destroy)

    # ------------------------------------------------------------------ 外壳
    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=T.PAGE_PAD, pady=(T.GAP_LG, T.GAP_SM))

        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(
            titles, text="设置", font=T.font(T.SIZE_DISPLAY, "bold"), text_color=T.TEXT, anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="两种运行方式的参数与连接测试都集中在这一页；保存后立即生效。",
            font=T.font(T.SIZE_SMALL),
            text_color=T.TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        actions = ctk.CTkFrame(head, fg_color="transparent")
        actions.pack(side="right")
        T.ghost(actions, "打开 config.json", self.app.open_config_file, height=34, width=126).pack(
            side="left"
        )
        T.ghost(actions, "←  返回首页", self.app.show_home, height=34, width=112).pack(
            side="left", padx=(T.GAP_SM, 0)
        )

        self.tabs = ctk.CTkTabview(
            self,
            fg_color=T.SURFACE,
            corner_radius=T.CARD_RADIUS,
            border_width=1,
            border_color=T.BORDER,
            segmented_button_fg_color=T.SURFACE_ALT,
            segmented_button_selected_color=T.ACCENT,
            segmented_button_selected_hover_color=T.ACCENT_HOVER,
            segmented_button_unselected_color=T.SURFACE_ALT,
            segmented_button_unselected_hover_color=T.SURFACE_HOVER,
            segmented_button_font=T.font(T.SIZE_SMALL, "bold"),
            text_color=T.TEXT,
            anchor="w",
        )
        self.tabs.pack(fill="both", expand=True, padx=T.PAGE_PAD, pady=(0, T.PAGE_PAD))

        for mode in TAB_ORDER:
            item = feature(self.app.config, "modes", mode)
            if not item["visible"]:
                continue
            name = self._tab_name(mode, item)
            self.tabs.add(name)
            self._build_tab(self.tabs.tab(name), mode, item)

        first = self.tabs.get()
        if first:
            self.tabs.set(first)

    @staticmethod
    def _tab_name(mode: str, item: dict) -> str:
        hint = item.get("hint") or ""
        return f"{item.get('label', mode)} · {hint}" if hint else item.get("label", mode)

    def _tab_page(self, mode: str) -> Optional[ctk.CTkFrame]:
        item = feature(self.app.config, "modes", mode)
        if not item["visible"]:
            return None
        return self.tabs.tab(self._tab_name(mode, item))

    # ------------------------------------------------------------------ 单个 Tab
    def _build_tab(self, page: ctk.CTkFrame, mode: str, item: dict):
        scroll = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            scrollbar_button_color=T.BORDER_STRONG,
            scrollbar_button_hover_color=T.NEUTRAL,
        )
        scroll.pack(fill="both", expand=True, padx=2, pady=2)

        enabled = bool(item["enabled"])
        if not enabled:
            Banner(
                scroll,
                f"「{item['label']}」当前未启用，无法用于运行。"
                f"把 config.json → features.modes.{mode}.enabled 改成 true 并重启界面即可。",
                "warn",
            ).pack(fill="x", padx=6, pady=(4, T.GAP_SM))

        params = Card(scroll, "连接参数", self._params_hint(mode))
        params.pack(fill="x", padx=6, pady=(0, T.GAP_MD))
        entries: Dict[str, ctk.CTkEntry] = {}
        variables: Dict[str, ctk.Variable] = {}
        self._build_fields(params.body, mode, entries, variables)

        test_card = Card(scroll, "连接测试", "连一次后端并抓一帧，确认路径 / 窗口 / 分辨率是否对得上")
        test_card.pack(fill="x", padx=6, pady=(0, T.GAP_MD))

        buttons = ctk.CTkFrame(test_card.body, fg_color="transparent")
        buttons.pack(fill="x")
        test_btn = T.ghost(
            buttons,
            "🔌  测试连接",
            lambda: self._run_probe(mode),
            width=118,
            height=34,
            state="normal" if enabled else "disabled",
        )
        test_btn.pack(side="left")
        T.primary(buttons, "保存设置", self._on_save, width=104, height=34).pack(
            side="left", padx=(T.GAP_SM, 0)
        )
        status = ctk.CTkLabel(
            buttons,
            text="尚未测试" if enabled else "方案未启用，测试已停用",
            font=T.font(T.SIZE_SMALL),
            text_color=T.TEXT_FAINT,
            anchor="e",
        )
        status.pack(side="right")

        banner = Banner(test_card.body, "", "info")
        preview = ctk.CTkLabel(test_card.body, text="", image=None)

        self._ui[mode] = {
            "entries": entries,
            "variables": variables,
            "test_btn": test_btn,
            "status": status,
            "banner": banner,
            "preview": preview,
            "enabled": enabled,
        }
        self.sync_from_config(mode)

    @staticmethod
    def _params_hint(mode: str) -> str:
        return (
            "留空用系统 PATH 里的 adb；模拟器一般填 127.0.0.1:7555"
            if mode == "adb"
            else "先填窗口标题；找不到窗口时改用固定区域"
        )

    def _build_fields(self, body, mode: str, entries: Dict[str, ctk.CTkEntry], variables: Dict[str, ctk.Variable]):
        if mode == "adb":
            row = form_row(body, "ADB 路径")
            entries["adb_path"] = T.entry(row, placeholder_text="留空用系统 PATH，或填完整路径")
            entries["adb_path"].pack(side="left", fill="x", expand=True)

            row = form_row(body, "设备地址")
            entries["device"] = T.entry(row, placeholder_text="127.0.0.1:7555（MuMu）")
            entries["device"].pack(side="left", fill="x", expand=True)

            row = form_row(body, "分辨率")
            entries["resolution"] = T.entry(row, placeholder_text="留空自动获取", width=160)
            entries["resolution"].pack(side="left")
            ctk.CTkLabel(
                row,
                text="填 宽x高（如 1280x720），留空则读取 wm size",
                font=T.font(T.SIZE_TINY),
                text_color=T.TEXT_FAINT,
            ).pack(side="left", padx=(10, 0))
            return

        row = form_row(body, "窗口标题")
        entries["window_title"] = T.entry(row, placeholder_text="这城有良田 / WeChat")
        entries["window_title"].pack(side="left", fill="x", expand=True)

        row = form_row(body, "采集模式")
        variables["capture_mode"] = ctk.StringVar(value=list(CAPTURE_LABELS)[0])
        menu = T.option_menu(row, list(CAPTURE_LABELS), variables["capture_mode"], width=150)
        menu.pack(side="left")
        ctk.CTkLabel(
            row,
            text="mss 更快；出现花屏 / 黑屏时换 PIL",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_FAINT,
        ).pack(side="left", padx=(10, 0))

        row = form_row(body, "固定区域")
        entries["region"] = T.entry(row, placeholder_text="x,y,w,h（可选，优先于窗口标题）", width=230)
        entries["region"].pack(side="left")
        ctk.CTkLabel(
            row,
            text="窗口定位不到时，直接写死屏幕上的绝对像素区域",
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_FAINT,
        ).pack(side="left", padx=(10, 0))

        row = form_row(body, "跟随窗口")
        variables["follow_window"] = ctk.BooleanVar(value=True)
        T.switch(row, "窗口移动时自动跟随（3 秒校准一次）", variables["follow_window"]).pack(side="left")

    # ------------------------------------------------------------------ 数据
    def sync_from_config(self, mode: Optional[str] = None):
        """配置文件 → 界面控件（切回设置页时重新对齐一次）。"""
        modes = [mode] if mode else list(self._ui)
        for name in modes:
            ui = self._ui.get(name)
            if not ui:
                continue
            cfg = self.app.config.get(name) or {}
            entries = ui["entries"]
            if "adb_path" in entries:
                _set_entry(entries["adb_path"], cfg.get("adb_path", ""))
            if "device" in entries:
                _set_entry(entries["device"], cfg.get("device", ""))
            if "resolution" in entries:
                _set_entry(entries["resolution"], cfg.get("resolution", ""))
            if "window_title" in entries:
                _set_entry(entries["window_title"], cfg.get("window_title", ""))
            if "region" in entries:
                region = cfg.get("region")
                text = ",".join(str(v) for v in region) if isinstance(region, (list, tuple)) else ""
                _set_entry(entries["region"], text)
            if "capture_mode" in ui["variables"]:
                ui["variables"]["capture_mode"].set(
                    CAPTURE_KEYS.get(str(cfg.get("capture_mode") or "mss"), list(CAPTURE_LABELS)[0])
                )
            if "follow_window" in ui["variables"]:
                ui["variables"]["follow_window"].set(bool(cfg.get("follow_window", True)))

    def collect_backend(self, mode: str) -> dict:
        ui = self._ui.get(mode)
        if not ui:
            return dict(self.app.config.get(mode) or {})
        entries = ui["entries"]
        if mode == "adb":
            return {
                "adb_path": entries["adb_path"].get().strip(),
                "device": entries["device"].get().strip(),
                "resolution": entries["resolution"].get().strip(),
            }
        region_text = entries["region"].get().strip().replace("，", ",")
        region: Optional[List[int]] = None
        if region_text:
            parts = [part.strip() for part in region_text.split(",")]
            if len(parts) == 4 and all(part.lstrip("-").isdigit() for part in parts):
                region = [int(part) for part in parts]
        return {
            "window_title": entries["window_title"].get().strip(),
            "capture_mode": CAPTURE_LABELS.get(
                ui["variables"]["capture_mode"].get(), "mss"
            ),
            "region": region,
            "follow_window": bool(ui["variables"]["follow_window"].get()),
        }

    def apply_to_config(self):
        for mode in self._ui:
            self.app.config[mode] = self.collect_backend(mode)

    def _on_save(self):
        self.apply_to_config()
        ok = self.app.persist_config()
        target = next(iter(self._ui.values()), None)
        if target is not None:
            target["status"].configure(
                text="已保存到 config.json" if ok else "保存失败，请检查文件权限",
                text_color=T.SUCCESS if ok else T.DANGER,
            )

    # ------------------------------------------------------------------ 测试
    def _run_probe(self, mode: str):
        ui = self._ui.get(mode)
        if not ui or not ui["enabled"]:
            return
        ui["test_btn"].configure(state="disabled")
        ui["status"].configure(text="正在连接…", text_color=T.ACCENT)
        ui["banner"].pack_forget()
        ui["preview"].pack_forget()

        backend = self.collect_backend(mode)

        def worker():
            from core.logger import Logger

            logger = Logger(
                log_dir=os.path.join(self.app.root_dir, "logs"),
                level="info",
                echo=False,
            )
            ok, message, path = probe(
                {
                    "mode": mode,
                    "backend": backend,
                    "templates_dir": os.path.join(self.app.root_dir, "templates"),
                },
                logger,
            )
            self._result_queue.put((mode, ok, message, path))

        threading.Thread(target=worker, daemon=True, name="zcylt-probe").start()
        self.after(120, self._poll_probe)

    def _poll_probe(self):
        if not self._alive:
            return
        try:
            mode, ok, message, path = self._result_queue.get_nowait()
        except queue.Empty:
            self.after(120, self._poll_probe)
            return
        self._show_probe_result(mode, ok, message, path)

    def _show_probe_result(self, mode: str, ok: bool, message: str, path: Optional[str]):
        ui = self._ui.get(mode)
        if not ui:
            return
        ui["test_btn"].configure(state="normal" if ui["enabled"] else "disabled")
        ui["status"].configure(
            text="连接正常" if ok else "连接失败",
            text_color=T.SUCCESS if ok else T.DANGER,
        )
        ui["banner"].set_text(message, "success" if ok else "error")
        ui["banner"].pack(fill="x", pady=(T.GAP_MD, 0))
        if ok and path:
            self._show_preview(ui["preview"], path)

    @staticmethod
    def _show_preview(label: ctk.CTkLabel, path: str):
        """抓到的帧缩一张小图，直观确认「框到的画面对不对」。"""
        try:
            from PIL import Image

            image = Image.open(path)
            max_w, max_h = 360, 200
            scale = min(max_w / image.width, max_h / image.height, 1.0)
            size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            image = image.resize(size, Image.LANCZOS)
            ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=size)
            label.configure(image=ctk_image, text="")
            label.image = ctk_image  # 必须留引用，否则被 GC 掉
            label.pack(anchor="w", pady=(T.GAP_MD, 0))
        except Exception:
            label.pack_forget()

    def _on_destroy(self, _event=None):
        self._alive = False


def _set_entry(widget: ctk.CTkEntry, value) -> None:
    widget.delete(0, "end")
    widget.insert(0, "" if value is None else str(value))
