"""通用控件：卡片、统计块、方案切换器、提示条。

页面文件只负责「摆内容」，形状和配色都在这里和 :mod:`ui.theme` 定。
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

import customtkinter as ctk

from ui import theme as T


class Card(ctk.CTkFrame):
    """带标题栏的白卡片。

    ``card.actions`` 是标题栏右侧的空容器，往里 pack 按钮即可（先 pack 的靠右）。
    ``card.body`` 是内容区。
    """

    def __init__(
        self,
        master,
        title: str = "",
        subtitle: str = "",
        padx: int = 16,
        pady: tuple = (14, 14),
        **kwargs,
    ):
        options = dict(
            fg_color=T.SURFACE,
            corner_radius=T.CARD_RADIUS,
            border_width=1,
            border_color=T.BORDER,
        )
        options.update(kwargs)
        super().__init__(master, **options)

        self.actions: Optional[ctk.CTkFrame] = None
        if title:
            head = ctk.CTkFrame(self, fg_color="transparent")
            head.pack(fill="x", padx=padx, pady=(pady[0], 0))
            ctk.CTkLabel(
                head, text=title, font=T.font(T.SIZE_TITLE, "bold"), text_color=T.TEXT, anchor="w"
            ).pack(side="left")
            if subtitle:
                ctk.CTkLabel(
                    head,
                    text=subtitle,
                    font=T.font(T.SIZE_SMALL),
                    text_color=T.TEXT_MUTED,
                    anchor="w",
                ).pack(side="left", padx=(10, 0), pady=(2, 0))
            self.actions = ctk.CTkFrame(head, fg_color="transparent")
            self.actions.pack(side="right")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(
            fill="both",
            expand=True,
            padx=padx,
            pady=(T.GAP_MD if title else 0, pady[1]),
        )


class StatTile(ctk.CTkFrame):
    """一个小块：大数字 + 说明，用来给页面做「一眼看懂」的概览。"""

    def __init__(self, master, label: str, value: str = "—", **kwargs):
        options = dict(fg_color=T.SURFACE, corner_radius=T.CARD_RADIUS, border_width=1, border_color=T.BORDER)
        options.update(kwargs)
        super().__init__(master, **options)
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=(11, 10))
        self._value = ctk.CTkLabel(
            inner,
            text=value,
            font=T.font(T.SIZE_DISPLAY, "bold"),
            text_color=T.TEXT,
            anchor="w",
        )
        self._value.pack(anchor="w")
        ctk.CTkLabel(
            inner,
            text=label,
            font=T.font(T.SIZE_TINY),
            text_color=T.TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))

    def set(self, value: str, color: T.Color | None = None):
        self._value.configure(text=str(value), text_color=color or T.TEXT)


class Banner(ctk.CTkFrame):
    """一条状态提示（提示 / 警告 / 错误 / 成功）。

    比弹窗温和：不打断操作，也不会被用户随手关掉后再也想不起来。
    """

    _TONES = {
        "info": (T.ACCENT_SOFT, T.ACCENT, "i"),
        "warn": (T.WARN_SOFT, T.WARN, "!"),
        "error": (T.DANGER_SOFT, T.DANGER, "×"),
        "success": (T.SUCCESS_SOFT, T.SUCCESS, "✓"),
    }

    def __init__(self, master, text: str = "", kind: str = "info", wraplength: int = 720):
        tone, self._fg_color, glyph = self._TONES.get(kind, self._TONES["info"])
        super().__init__(master, fg_color=tone, corner_radius=8)
        self._glyph = ctk.CTkLabel(
            self,
            text=glyph,
            width=18,
            font=T.font(T.SIZE_SMALL, "bold"),
            text_color=self._fg_color,
        )
        self._glyph.pack(side="left", padx=(10, 0), pady=7, anchor="n")
        self._label = ctk.CTkLabel(
            self,
            text=text,
            font=T.font(T.SIZE_SMALL),
            text_color=self._fg_color,
            justify="left",
            anchor="w",
            wraplength=wraplength,
        )
        self._label.pack(side="left", padx=(6, 12), pady=7, fill="x", expand=True)

    def set_text(self, text: str, kind: Optional[str] = None):
        if kind and kind in self._TONES:
            tone, self._fg_color, glyph = self._TONES[kind]
            self.configure(fg_color=tone)
            self._glyph.configure(text=glyph, text_color=self._fg_color)
            self._label.configure(text_color=self._fg_color)
        self._label.configure(text=text)


class Tag(ctk.CTkLabel):
    """小圆角标签：启用 / 关闭 / 缺失 / 当前方案。"""

    def __init__(self, master, text: str, fg: T.Color = T.NEUTRAL_SOFT, color: T.Color = T.TEXT_MUTED):
        super().__init__(
            master,
            text=text,
            height=20,
            font=T.font(T.SIZE_TINY),
            text_color=color,
            fg_color=fg,
            corner_radius=7,
            padx=7,
        )


class ModeSwitcher(ctk.CTkFrame):
    """顶栏的方案切换器。

    没启用的方案仍然渲染（灰着），点一下会说明原因——
    直接消失的话用户只会觉得「功能丢了」。
    """

    def __init__(
        self,
        master,
        modes: Iterable[dict],
        command: Callable[[str], None],
        disabled_command: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(master, fg_color=T.SURFACE_ALT, corner_radius=10, border_width=1, border_color=T.BORDER)
        self._command = command
        self._disabled_command = disabled_command
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._modes: dict[str, dict] = {}

        for mode in modes:
            key = mode["key"]
            enabled = bool(mode.get("enabled", True))
            text = mode.get("label", key) if enabled else f"{mode.get('label', key)} · 未启用"
            width = max(78, int(T.SIZE_SMALL * 1.15 * len(text)) + 22)
            button = ctk.CTkButton(
                self,
                text=text,
                width=width,
                height=30,
                corner_radius=7,
                fg_color="transparent",
                hover_color=T.SURFACE_HOVER if enabled else T.SURFACE_ALT,
                text_color=T.TEXT if enabled else T.TEXT_FAINT,
                font=T.font(T.SIZE_SMALL, "bold" if enabled else "normal"),
                border_width=0,
                command=lambda k=key, ok=enabled: self._on_click(k, ok),
            )
            button.pack(side="left", padx=3, pady=3)
            self._buttons[key] = button
            self._modes[key] = mode

    def _on_click(self, key: str, enabled: bool):
        if enabled:
            self._command(key)
        elif self._disabled_command is not None:
            self._disabled_command(key)

    def set_active(self, key: Optional[str]):
        for name, button in self._buttons.items():
            mode = self._modes[name]
            enabled = bool(mode.get("enabled", True))
            if name == key and enabled:
                button.configure(fg_color=T.SURFACE, text_color=T.ACCENT, hover_color=T.SURFACE)
            else:
                button.configure(
                    fg_color="transparent",
                    hover_color=T.SURFACE_HOVER if enabled else T.SURFACE_ALT,
                    text_color=T.TEXT if enabled else T.TEXT_FAINT,
                )


def form_row(master, label: str, label_width: int = 84) -> ctk.CTkFrame:
    """一行「标签 + 输入控件」的左半边；调用方接着 pack 输入框。"""
    row = ctk.CTkFrame(master, fg_color="transparent")
    row.pack(fill="x", pady=5)
    ctk.CTkLabel(
        row,
        text=label,
        width=label_width,
        anchor="w",
        font=T.font(T.SIZE_BODY),
        text_color=T.TEXT_MUTED,
    ).pack(side="left")
    return row


def hint_label(master, text: str, wraplength: int = 420) -> ctk.CTkLabel:
    """输入框右侧的灰色说明。"""
    return ctk.CTkLabel(
        master,
        text=text,
        font=T.font(T.SIZE_TINY),
        text_color=T.TEXT_FAINT,
        anchor="w",
        justify="left",
        wraplength=wraplength,
    )
