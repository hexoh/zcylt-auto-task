"""设计令牌：颜色、字体、间距、按钮样式。

界面里出现的每个颜色都从这里取，页面文件不再直接写十六进制——
想整体换色只改这一处，也避免出现「同一个蓝有五种」。

颜色全部写成 ``(浅色, 深色)`` 元组，跟随系统外观切换。
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple

import customtkinter as ctk

Color = Tuple[str, str] | str

# ---------------------------------------------------------------------- 字体
def _ui_font_family() -> str:
    if sys.platform == "darwin":
        return "PingFang SC"
    if sys.platform.startswith("win"):
        return "Microsoft YaHei UI"
    return "Noto Sans CJK SC"


def _mono_font_family() -> str:
    if sys.platform == "darwin":
        return "Menlo"
    if sys.platform.startswith("win"):
        return "Consolas"
    return "DejaVu Sans Mono"


UI_FONT = _ui_font_family()
MONO_FONT = _mono_font_family()

# 字号刻度：只有这五档，避免每处随手写 size=13/14/15
SIZE_DISPLAY = 19  # 页面主标题
SIZE_TITLE = 15  # 卡片标题
SIZE_BODY = 13  # 正文 / 输入框
SIZE_SMALL = 12  # 辅助说明
SIZE_TINY = 11  # 标签、路径


def font(size: int = SIZE_BODY, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


def mono(size: int = SIZE_TINY, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=MONO_FONT, size=size, weight=weight)


# ---------------------------------------------------------------------- 颜色
BG = ("#F2F4F8", "#121317")  # 窗口底色
SURFACE = ("#FFFFFF", "#1C1D22")  # 卡片 / 顶栏
SURFACE_ALT = ("#F5F7FB", "#23242B")  # 卡片内的次级块、输入框底
SURFACE_HOVER = ("#EDF1F8", "#2A2C34")
BORDER = ("#E2E7F0", "#2B2D36")
BORDER_STRONG = ("#CBD3E1", "#3A3D48")

TEXT = ("#16181D", "#F2F3F7")
TEXT_MUTED = ("#68707E", "#9AA1AC")
TEXT_FAINT = ("#8D96A5", "#7A828E")

ACCENT = ("#2F6BE6", "#4C86F5")
ACCENT_HOVER = ("#2558C2", "#3E74E0")
ACCENT_SOFT = ("#E9F0FE", "#222F4A")
ACCENT_TEXT = "#FFFFFF"

SUCCESS = ("#0E8A57", "#45D08D")
SUCCESS_SOFT = ("#E4F7EE", "#183328")
WARN = ("#A46800", "#F0B45C")
WARN_SOFT = ("#FCF1E1", "#3A2C19")
DANGER = ("#C23B3B", "#EC7B7B")
DANGER_SOFT = ("#FBEBEB", "#3B2323")
NEUTRAL = ("#7A828E", "#8C939E")
NEUTRAL_SOFT = ("#EDF0F6", "#2A2C34")

# ---------------------------------------------------------------------- 间距
GAP_XS = 4
GAP_SM = 8
GAP_MD = 12
GAP_LG = 18
GAP_XL = 24
PAGE_PAD = 22
CARD_RADIUS = 12
CONTROL_RADIUS = 8


# ---------------------------------------------------------------------- 按钮
def primary(master, text: str, command, **kwargs) -> ctk.CTkButton:
    options = dict(
        text=text,
        command=command,
        height=38,
        corner_radius=CONTROL_RADIUS,
        fg_color=ACCENT,
        hover_color=ACCENT_HOVER,
        text_color=ACCENT_TEXT,
        font=font(SIZE_BODY, "bold"),
        border_width=0,
    )
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def danger(master, text: str, command, **kwargs) -> ctk.CTkButton:
    options = dict(
        text=text,
        command=command,
        height=38,
        corner_radius=CONTROL_RADIUS,
        fg_color=DANGER,
        hover_color=("#A93232", "#D96A6A"),
        text_color="#FFFFFF",
        font=font(SIZE_BODY, "bold"),
        border_width=0,
    )
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def ghost(master, text: str, command, **kwargs) -> ctk.CTkButton:
    """次级按钮：白底细描边，不抢主按钮的视觉重心。"""
    options = dict(
        text=text,
        command=command,
        height=32,
        corner_radius=CONTROL_RADIUS,
        fg_color=SURFACE,
        hover_color=SURFACE_HOVER,
        text_color=TEXT,
        font=font(SIZE_SMALL),
        border_width=1,
        border_color=BORDER_STRONG,
    )
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def text_button(master, text: str, command, **kwargs) -> ctk.CTkButton:
    """纯文字按钮：用于「清空」「打开目录」这类不重要的动作。"""
    options = dict(
        text=text,
        command=command,
        height=26,
        corner_radius=6,
        fg_color="transparent",
        hover_color=SURFACE_HOVER,
        text_color=TEXT_MUTED,
        font=font(SIZE_SMALL),
        border_width=0,
    )
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


# ---------------------------------------------------------------------- 其它
def hline(master, **pack_kwargs) -> ctk.CTkFrame:
    """1px 分隔线。"""
    line = ctk.CTkFrame(master, height=1, fg_color=BORDER, corner_radius=0)
    line.pack(fill="x", **pack_kwargs)
    return line


def label(master, text: str, size: int = SIZE_BODY, color: Color = TEXT, **kwargs) -> ctk.CTkLabel:
    options = dict(text=text, text_color=color, font=font(size), anchor="w")
    options.update(kwargs)
    return ctk.CTkLabel(master, **options)


def entry(master, placeholder: str = "", width: Optional[int] = None, **kwargs) -> ctk.CTkEntry:
    options = dict(
        placeholder_text=placeholder,
        height=34,
        corner_radius=CONTROL_RADIUS,
        border_width=1,
        border_color=BORDER_STRONG,
        fg_color=SURFACE_ALT,
        text_color=TEXT,
        placeholder_text_color=TEXT_FAINT,
        font=font(SIZE_BODY),
    )
    if width:
        options["width"] = width
    options.update(kwargs)
    return ctk.CTkEntry(master, **options)


def option_menu(master, values, variable=None, width: int = 150, **kwargs) -> ctk.CTkOptionMenu:
    options = dict(
        values=list(values) or ["—"],
        variable=variable,
        width=width,
        height=34,
        corner_radius=CONTROL_RADIUS,
        fg_color=SURFACE_ALT,
        button_color=SURFACE_ALT,
        button_hover_color=SURFACE_HOVER,
        text_color=TEXT,
        font=font(SIZE_BODY),
        dropdown_fg_color=SURFACE,
        dropdown_hover_color=SURFACE_HOVER,
        dropdown_text_color=TEXT,
        dropdown_font=font(SIZE_SMALL),
    )
    options.update(kwargs)
    return ctk.CTkOptionMenu(master, **options)


def switch(master, text: str, variable, **kwargs) -> ctk.CTkSwitch:
    options = dict(
        text=text,
        variable=variable,
        font=font(SIZE_SMALL),
        text_color=TEXT,
        progress_color=ACCENT,
        button_color=("#FFFFFF", "#E7E9EF"),
        button_hover_color="#FFFFFF",
        fg_color=BORDER_STRONG,
        width=40,
    )
    options.update(kwargs)
    return ctk.CTkSwitch(master, **options)
