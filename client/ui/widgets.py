# -*- coding: utf-8 -*-
# This file is part of MCSync.
# Copyright (C) 2025 radix-Lunaris
#
# MCSync is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# MCSync is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along
# with MCSync. If not, see <https://www.gnu.org/licenses/>.

"""可复用的现代化卡片与标签组件"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from core.models import Instance, SaveInfo
from core.protocol import human_size
from ui import theme
from core.scanner import human_time_ago
from core.i18n import t


class Card(ctk.CTkFrame):
    """带悬停与选中态的卡片容器（点击穿透到所有子控件）"""

    def __init__(self, master, on_click: Optional[Callable] = None, on_double=None,
                 selectable: bool = True, padding: int = 10, **kwargs):
        kwargs.setdefault("corner_radius", 12)
        kwargs.setdefault("border_width", 1)
        super().__init__(master, **kwargs)
        self.on_click = on_click
        self.on_double = on_double
        self.selectable = selectable
        self.padding = padding
        self._selected = False
        self._normal = None
        self.after(60, self._init_colors)

    def _init_colors(self):
        c = theme.colors()
        self._normal = c["card"]
        if self._selected:
            self.configure(fg_color=c["card2"], border_color=c["accent"])
        else:
            self.configure(fg_color=self._normal, border_color=c["line"])

    # ---------------------------------------------------------- 交互
    def bind_click(self, widget=None):
        w = widget or self
        if self.on_click:
            w.bind("<Button-1>", lambda e: self.on_click())
        if self.on_double:
            w.bind("<Double-Button-1>", lambda e: self.on_double())
        for ch in w.winfo_children():
            self.bind_click(ch)

    def set_selected(self, on: bool) -> None:
        self._selected = on
        c = theme.colors()
        if on:
            self.configure(fg_color=c["card2"], border_color=c["accent"])
        else:
            self.configure(fg_color=self._normal or c["card"], border_color=c["line"])


def pill(master, text: str, color: Optional[str] = None, small: bool = True) -> ctk.CTkLabel:
    """小徽章标签"""
    c = theme.colors()
    fg = color or c["accent"]
    return ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(9 if small else 10, "bold"),
        text_color=fg,
        fg_color=(_mix(c["card2"], fg, 0.14)),
        corner_radius=7,
        padx=6,
        pady=1,
    )


def _mix(base: str, other: str, ratio: float) -> str:
    """把 base 向 other 混合（用于生成柔和的徽章底色），支持明暗两种模式传入 tuple"""
    try:
        b = base.lstrip("#")
        o = other.lstrip("#")
        br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        or_, og, ob = int(o[0:2], 16), int(o[2:4], 16), int(o[4:6], 16)
        r = int(br + (or_ - br) * ratio)
        g = int(bg + (og - bg) * ratio)
        bl = int(bb + (ob - bb) * ratio)
        return "#%02x%02x%02x" % (r, g, bl)
    except Exception:
        return base


def section_header(master, title: str, subtitle: str = "", right=None) -> ctk.CTkFrame:
    """分区标题栏"""
    f = ctk.CTkFrame(master, fg_color="transparent")
    ctk.CTkLabel(f, text=title, font=theme.font(14, "bold")).pack(side="left", padx=(10, 6), pady=(8, 4))
    if subtitle:
        ctk.CTkLabel(f, text=subtitle, font=theme.font(11), text_color=theme.colors()["muted"]).pack(
            side="left", pady=(8, 4))
    if right is not None:
        right.pack(in_=f, side="right", padx=8)
    return f


class InstanceCard(Card):
    ROW = 44

    def __init__(self, master, inst: Instance, on_click: Optional[Callable] = None):
        super().__init__(master, on_click=on_click, padding=8)
        self.inst = inst
        c = theme.colors()
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(6, 8), pady=6)
        ctk.CTkLabel(left, text=_instance_icon(inst), font=theme.font(17)).pack(anchor="center")
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, pady=5)
        ctk.CTkLabel(body, text=inst.name, font=theme.font(11, "bold"), anchor="w",
                     wraplength=170).pack(fill="x")
        sub = f"{inst.source}"
        if inst.game_version:
            sub += f" · {inst.game_version}"
        ctk.CTkLabel(body, text=sub, font=theme.font(9), text_color=c["muted"],
                     anchor="w", wraplength=180).pack(fill="x")
        self.bind_click()

    def refresh_theme(self):
        self._init_colors()


class SaveCard(Card):
    def __init__(self, master, save: SaveInfo, on_click: Optional[Callable] = None,
                 on_double: Optional[Callable] = None):
        super().__init__(master, on_click=on_click, on_double=on_double, padding=10)
        self.save = save
        c = theme.colors()
        self.grid_columnconfigure(1, weight=1)

        # 图标
        icon_label = ctk.CTkLabel(self, text="", width=44, height=44)
        icon_label.grid(row=0, column=0, rowspan=3, padx=(8, 10), pady=6)
        self._set_icon(icon_label)

        # 标题
        title = ctk.CTkLabel(self, text=save.display_name, font=theme.font(12, "bold"), anchor="w")
        title.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 0))

        # 路径
        ctk.CTkLabel(self, text=save.name, font=theme.font(9), text_color=c["muted"],
                     anchor="w", wraplength=420).grid(row=1, column=1, sticky="ew", padx=(0, 8))

        # 徽章行
        badges = ctk.CTkFrame(self, fg_color="transparent")
        badges.grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=(4, 8))
        if save.game_version:
            pill(badges, save.game_version).pack(side="left", padx=(0, 5))
        if save.gametype:
            pill(badges, save.gametype, c["ok"]).pack(side="left", padx=(0, 5))
        if save.difficulty:
            pill(badges, save.difficulty, c["warn"]).pack(side="left", padx=(0, 5))
        if save.hardcore:
            pill(badges, t("极限"), c["danger"]).pack(side="left", padx=(0, 5))
        self._pill_size = pill(badges, f"💾 {save.size_text}")
        self._pill_size.pack(side="left", padx=(0, 5))
        pill(badges, f"🕒 {human_time_ago(save.last_played)}").pack(side="left", padx=(0, 5))

        # 右侧状态
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=3, padx=(4, 10))
        if save.synced:
            # 显示上次同步时间，不再显示 vN —— 版本序号是存档内部概念，
            # 多个整合包并排时没有可比性，用户关心的是"这存档哪天传的"。
            _ts = float(getattr(save.state, "last_sync", 0) or 0)
            _when = (time.strftime("%m-%d %H:%M", time.localtime(_ts))
                     if _ts else human_time_ago(0))
            pill(right, f"☁ 已同步 · {_when}", c["ok"]).pack(anchor="e", pady=(10, 3))
        else:
            pill(right, t("未同步"), c["muted"]).pack(anchor="e", pady=(10, 3))
        ctk.CTkLabel(right, text=f"{save.file_count if save.file_count >= 0 else '—'} 文件",
                     font=theme.font(9), text_color=c["muted"]).pack(anchor="e")

        self.bind_click()

    def refresh_size(self):
        """只更新体积数字，不重建卡片（避免选中态与滚动位置丢失）。"""
        try:
            self._pill_size.configure(text=f"💾 {self.save.size_text}")
        except Exception:
            pass

    def _set_icon(self, label: ctk.CTkLabel):
        img = None
        if self.save.icon and os.path.exists(self.save.icon):
            try:
                im = Image.open(self.save.icon).convert("RGBA").resize((44, 44), Image.LANCZOS)
                img = ctk.CTkImage(light_image=im, dark_image=im, size=(44, 44))
            except Exception:
                img = None
        if img is not None:
            label.configure(image=img)
        else:
            label.configure(text="🗺", font=theme.font(21), text_color=theme.colors()["accent"])


class CloudCard(Card):
    """云端存档卡片。

    显示「整合包名 · 存档名 + 更新时间」，不再显示 v1/v2——
    版本号是单个存档内部的序号，不同存档的 v1 互不相干，
    并列展示会让人误以为它们是同一套版本。
    """

    def __init__(self, master, data: dict, on_click: Optional[Callable] = None,
                 on_manage: Optional[Callable] = None):
        super().__init__(master, on_click=on_click, padding=10)
        self.data = data
        c = theme.colors()
        self.grid_columnconfigure(0, weight=1)

        inst = (data.get("instance", "") or "").strip()
        # display 是"去掉整合包限定后缀"的原始存档名；老数据没有该字段时退回 name
        name = data.get("display") or data.get("name", "")

        # 行1：整合包名（若有），小字弱化
        if inst:
            ctk.CTkLabel(self, text=f"📦 {inst}", font=theme.font(9),
                         text_color=c["muted"], anchor="w",
                         wraplength=300).grid(row=0, column=0, sticky="ew",
                                              padx=(10, 6), pady=(7, 0))

        # 行2：存档名（主标题）
        ctk.CTkLabel(self, text=name, font=theme.font(12, "bold"), anchor="w",
                     wraplength=300).grid(row=1, column=0, sticky="ew",
                                          padx=(10, 6), pady=((2 if inst else 8), 0))

        # 行3：更新时间（替代原来的 vN）
        upd = _ts(data.get("latest_time") or data.get("updated_at"))
        if data.get("kind") == "config":
            upd = f"⚙ 整合包配置 · {upd}"
        ctk.CTkLabel(self, text=f"🕒 {upd}", font=theme.font(9),
                     text_color=c["muted"], anchor="w").grid(
            row=2, column=0, sticky="ew", padx=(10, 6), pady=(0, 1))

        # 行4：容量信息（版本号移到次要位置，仅在有多版本时提示）
        vcount = int(data.get("version_count", 0) or 0)
        tail = f"💾 {human_size(data.get('total_size', 0))} · 📄 {data.get('file_count', 0)} 文件"
        if vcount > 1:
            tail += f" · {vcount} 个历史版本"
        # 多人共用时，知道"这份是谁传的"很关键
        who = (data.get("last_user") or "").strip()
        if who:
            tail += f" · 👤 {who}"
        ctk.CTkLabel(self, text=tail, font=theme.font(9), text_color=c["muted"],
                     anchor="w", wraplength=300).grid(
            row=3, column=0, sticky="ew", padx=(10, 6), pady=(0, 8))
        if on_manage:
            btn = ctk.CTkButton(self, text=t("版本"), width=52, height=26, font=theme.font(11),
                                command=on_manage, fg_color="transparent",
                                border_width=1, border_color=c["line"], text_color=c["text"])
            btn.grid(row=0, column=1, rowspan=4, padx=(0, 10))
        self.bind_click()


def _instance_icon(inst: Instance) -> str:
    s = (inst.source or "").lower()
    if "pcl" in s:
        return "🟩"
    if "hmcl" in s:
        return "🟦"
    if "prism" in s or "multimc" in s:
        return "🟪"
    if "modrinth" in s:
        return "🟧"
    if "官方" in s:
        return "🟫"
    return "📁"


def _ts(ts) -> str:
    if not ts:
        return "未知"
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
