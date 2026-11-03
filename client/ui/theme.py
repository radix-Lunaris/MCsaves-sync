# -*- coding: utf-8 -*-
"""主题、字体与分辨率自适应"""
from __future__ import annotations

import platform

import customtkinter as ctk
from customtkinter import CTkFont

FONT_FAMILY = "Microsoft YaHei UI"

# ---------------------------------------------------------------- 配色
# 真正的令牌定义在 ui/design.py（含 8pt 间距、字号、圆角刻度）。
# 这里只做桥接：对外仍然提供 colors()，但键值统一来自 design，
# 避免两套配色并存导致"改了一处、另一处还是旧的"。
#
# 兼容旧键名：老代码里用的是 card / card2 / accent_hover 等，
# 全部映射到新令牌上，这样历史代码不必一次性改完也不会取不到值。
from ui import design as _ds  # noqa: E402


def is_dark() -> bool:
    try:
        return str(ctk.get_appearance_mode()).lower() == "dark"
    except Exception:
        return True


def colors() -> dict:
    """当前主题的配色令牌（新键名 + 旧键名兼容）。"""
    c = dict(_ds.tokens(is_dark()))
    # 旧键 -> 新键的兼容映射
    c.setdefault("card", c["surface"])
    c.setdefault("card2", c["surface2"])
    c.setdefault("accent_hover", c["accent_h"])
    c.setdefault("accent2", c["accent"])
    c.setdefault("sub", c["text2"])
    c.setdefault("hover", c["surface3"])
    c.setdefault("border", c["line"])
    c.setdefault("fg", c["text"])
    return c


def font(size: int = 13, weight: str = "normal") -> CTkFont:
    w = "bold" if weight in ("bold", "bold_italic") else "normal"
    try:
        return CTkFont(family=FONT_FAMILY, size=size, weight=w)
    except Exception:
        return CTkFont(size=size, weight=w)


def apply(mode: str = "dark", scale: float = 1.0) -> None:
    ctk.set_appearance_mode(mode if mode in ("dark", "light", "system") else "dark")
    ctk.set_default_color_theme("blue")
    try:
        ctk.set_widget_scaling(float(scale))
        ctk.set_spacing_scaling(float(scale))
    except Exception:
        pass


def auto_scale(root=None) -> float:
    """
    根据屏幕分辨率自动计算控件缩放，保证 1366×768 ~ 4K 都有舒适观感。
    """
    try:
        import ctypes
        if platform.system() == "Windows":
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
    except Exception:
        pass
    try:
        w = root.winfo_screenwidth() if root is not None else 1920
        h = root.winfo_screenheight() if root is not None else 1080
    except Exception:
        w, h = 1920, 1080
    s = min(w / 1600.0, h / 900.0)
    return round(max(0.8, min(1.6, s)), 2)


def enable_dpi_awareness() -> None:
    """Windows 高 DPI：让 Tk 使用系统 DPI 缩放，避免模糊/错位"""
    try:
        if platform.system() == "Windows":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
