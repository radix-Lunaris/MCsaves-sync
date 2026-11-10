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

"""对话框：设置 / 冲突解决 / 版本管理 / 通用确认"""
from __future__ import annotations

import os
from tkinter import filedialog
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from core.config import cfg
from core.protocol import DEFAULT_IGNORE_RULES, human_size
from ui import theme
from core.i18n import t
from core import i18n


class _BaseDialog(ctk.CTkToplevel):
    def __init__(self, master, title: str, width: int = 720, height: int = 560):
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=theme.colors()["card"])
        self.master_window = master
        self.result = None
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()
        self._center(width, height)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _center(self, w: int, h: int):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(w, int(sw * 0.9))
        h = min(h, int(sh * 0.9))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(int(w * 0.7), int(h * 0.6))

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ============================================================================
#  冲突解决
# ============================================================================
class ConflictDialog(_BaseDialog):
    """
    返回 {path: 'local'|'cloud'|'both'}，取消返回 None
    both = 保留云端副本（另存为 _冲突副本/），同时上传/下载本地版本
    """

    def __init__(self, master, conflicts: List[dict], direction: str = "upload"):
        super().__init__(master, "解决同步冲突", 640, 520)
        self.conflicts = conflicts
        self.direction = direction
        self.choices: Dict[str, ctk.CTkOptionMenu] = {}
        c = theme.colors()

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text=t("⚠ 检测到文件冲突"), font=theme.font(16, "bold")).pack(anchor="w")
        tip = ("这些文件在你本地和云端都被修改过，请选择每个文件的处理方式。"
               if direction == "upload" else
               "这些文件在你本地和云端都被修改过，请选择下载时如何处理。")
        ctk.CTkLabel(head, text=tip, font=theme.font(11), text_color=c["muted"], wraplength=600,
                     justify="left").pack(anchor="w", pady=(2, 0))

        # 批量操作
        bulk = ctk.CTkFrame(self, fg_color=c["card2"], corner_radius=10)
        bulk.pack(fill="x", padx=16, pady=(6, 8))
        ctk.CTkLabel(bulk, text=t("全部设为："), font=theme.font(12)).pack(side="left", padx=(12, 8), pady=8)
        for label, val in (("用本地", "local"), ("用云端", "cloud"), ("都保留", "both")):
            ctk.CTkButton(bulk, text=label, width=80, height=28, font=theme.font(12),
                          fg_color="transparent", border_width=1, border_color=c["line"],
                          text_color=c["text"],
                          command=lambda v=val: self._set_all(v)).pack(side="left", padx=4, pady=8)

        # 列表
        self.list = ctk.CTkScrollableFrame(self, fg_color=c["card2"], corner_radius=10)
        self.list.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.list.grid_columnconfigure(0, weight=1)
        for i, cf in enumerate(conflicts):
            self._row(i, cf)

        # 底部
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(bar, text=t("取消"), width=100, height=34, fg_color="transparent",
                      border_width=1, border_color=c["line"], text_color=c["text"],
                      font=theme.font(12), command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("确定，继续同步"), width=140, height=34, font=theme.font(12, "bold"),
                      command=self._ok).pack(side="right")

    def _row(self, i: int, cf: dict):
        c = theme.colors()
        row = ctk.CTkFrame(self.list, fg_color="transparent")
        row.grid(row=i, column=0, sticky="ew", padx=8, pady=4)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=cf.get("path", ""), font=theme.font(12, "bold"),
                     anchor="w", wraplength=380, justify="left").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            row,
            text=f"本地 {str(cf.get('local_hash', ''))[:10]} · 云端 {str(cf.get('cloud_hash', ''))[:10]}",
            font=theme.font(10), text_color=c["muted"], anchor="w",
        ).grid(row=1, column=0, sticky="w")
        opt = ctk.CTkOptionMenu(row, values=["用本地", "用云端", "都保留"], width=110, height=28,
                                font=theme.font(12), dropdown_font=theme.font(12))
        opt.set("用本地")
        opt.grid(row=0, column=1, rowspan=2, padx=(10, 4))
        self.choices[cf["path"]] = opt

    def _set_all(self, val: str):
        mapping = {"local": "用本地", "cloud": "用云端", "both": "都保留"}
        for opt in self.choices.values():
            opt.set(mapping[val])

    def _ok(self):
        rev = {"用本地": "local", "用云端": "cloud", "都保留": "both"}
        self.result = {p: rev[o.get()] for p, o in self.choices.items()}
        self._close()


# ============================================================================
#  版本管理
# ============================================================================
class VersionDialog(_BaseDialog):
    """云端版本列表：下载 / 回滚 / 删除。

    版本 id 只用于接口定位，界面上一律用「上传时间」标识 ——
    v1/v2 是单个存档内部的序号，跨存档/整合包并排时没有可比性，
    用户真正关心的是"哪一版是哪天传的"。
    """

    def __init__(self, master, cloud_name: str, versions: List[dict],
                 on_action: Optional[Callable[[str, int, str], None]] = None):
        super().__init__(master, f"版本管理 · {cloud_name}", 620, 520)
        self.on_action = on_action
        c = theme.colors()
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(head, text=f"☁ {cloud_name}", font=theme.font(16, "bold")).pack(side="left")
        ctk.CTkLabel(head, text=f"共 {len(versions)} 次上传", font=theme.font(11),
                     text_color=c["muted"]).pack(side="left", padx=10)

        box = ctk.CTkScrollableFrame(self, fg_color=c["card2"], corner_radius=10)
        box.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        box.grid_columnconfigure(0, weight=1)
        for i, v in enumerate(sorted(versions, key=lambda x: x["id"], reverse=True)):
            self._row(box, i, v)

    def _row(self, box, i: int, v: dict):
        c = theme.colors()
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.grid(row=i, column=0, sticky="ew", padx=8, pady=4)
        row.grid_columnconfigure(1, weight=1)
        # 主标识 = 上传时间（不再出现 v1 / #1 这类递增序号）
        vtime = v.get("created_at_str", "") or "—"
        ctk.CTkLabel(row, text="🕒", font=theme.font(13), width=28).grid(
            row=0, column=0, rowspan=2)
        ctk.CTkLabel(row, text=vtime, font=theme.font(12, "bold"), anchor="w").grid(
            row=0, column=1, sticky="w")
        sub = f"{v.get('file_count', 0)} 文件 · {human_size(v.get('total_size', 0))}"
        if v.get("note"):
            sub += f" · {v['note']}"
        ctk.CTkLabel(row, text=sub, font=theme.font(10), text_color=c["muted"], anchor="w").grid(
            row=1, column=1, sticky="w")
        ctk.CTkButton(row, text=t("下载"), width=56, height=26, font=theme.font(11),
                      command=lambda: self._act("download", v["id"], vtime)).grid(row=0, column=2, rowspan=2, padx=3)
        ctk.CTkButton(row, text=t("回滚"), width=56, height=26, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"], text_color=c["text"],
                      command=lambda: self._act("restore", v["id"], vtime)).grid(row=0, column=3, rowspan=2, padx=3)
        ctk.CTkButton(row, text=t("删除"), width=56, height=26, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["danger"],
                      text_color=c["danger"], hover_color="#7f1d1d",
                      command=lambda: self._act("delete", v["id"], vtime)).grid(row=0, column=4, rowspan=2, padx=3)

    def _act(self, action: str, vid: int, vtime: str = ""):
        if self.on_action:
            self.on_action(action, vid, vtime)
        if action in ("download", "restore"):
            self._close()


# ============================================================================
#  设置
# ============================================================================
class SettingsDialog(_BaseDialog):
    def __init__(self, master, on_saved: Optional[Callable] = None):
        super().__init__(master, "设置", 700, 600)
        self.on_saved = on_saved
        c = theme.colors()
        self.tab = ctk.CTkTabview(self, fg_color=c["card2"])
        self.tab.pack(fill="both", expand=True, padx=14, pady=14)
        for _active_tab in ("连接", "同步", "忽略规则", "高级", "关于"):
            self.tab.add(_active_tab)
        self._build_conn(self.tab.tab("连接"))
        self._build_sync(self.tab.tab("同步"))
        self._build_ignore(self.tab.tab("忽略规则"))
        self._build_adv(self.tab.tab("高级"))
        self._build_about(self.tab.tab("关于"))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 14))
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"], font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("保存"), width=100, height=34, font=theme.font(12, "bold"),
                      command=self._save).pack(side="right")

    # ------------------------------------------------------------ 连接
    def _build_conn(self, f):
        """连接设置页。

        布局要点：每个 grid 行只放一组控件，行号严格递增。
        旧版本把说明文字、代理开关、口令框塞进了重复的行号
        （row=1 与 row=4 各被两个控件占用），后放置的控件会盖住先放置的，
        表现就是界面重叠、口令框点不到也输入不进去。
        """
        c = theme.colors()
        f.grid_columnconfigure(0, minsize=118)
        f.grid_columnconfigure(1, weight=1)
        r = 0

        # ---- 服务器地址
        ctk.CTkLabel(f, text=t("服务器地址"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=(14, 4))
        self.e_url = ctk.CTkEntry(f, placeholder_text="http://1.2.3.4:36854",
                                  font=theme.font(12), height=34)
        self.e_url.grid(row=r, column=1, sticky="ew", padx=14, pady=(14, 4))
        self.e_url.insert(0, cfg.get("server_url", ""))
        r += 1

        ctk.CTkLabel(f, text=t("示例：http://1.2.3.4:36854 ；局域网填内网 IP，公网建议加 HTTPS 反代"),
                     font=theme.font(10), text_color=c["muted"], wraplength=440,
                     justify="left").grid(row=r, column=1, sticky="w", padx=14)
        r += 1

        # ---- 网络：客户端始终直连
        ctk.CTkLabel(f, text=t("网络方式"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=(8, 4))
        ctk.CTkLabel(f, text=t("直连（客户端不走任何代理）"),
                     font=theme.font(12), anchor="w").grid(
            row=r, column=1, sticky="w", padx=14, pady=(8, 4))
        r += 1
        ctk.CTkLabel(f, text="客户端连的是你自己的服务器，不需要代理。"
                             "若连不上请检查地址、端口与防火墙（云服务器需放行端口）。",
                     font=theme.font(10), text_color=c["muted"], wraplength=440,
                     justify="left").grid(row=r, column=1, sticky="w", padx=14)
        r += 1

        # ---- 服务器口令（独占一格，确保可点击可输入）
        ctk.CTkLabel(f, text=t("登录账号"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=(8, 4))
        self.e_user = ctk.CTkEntry(f, font=theme.font(12), height=34,
                                   placeholder_text=t("管理员分配给你的账号"))
        self.e_user.grid(row=r, column=1, sticky="ew", padx=14, pady=(8, 4))
        self.e_user.insert(0, cfg.get("username", ""))
        r += 1

        ctk.CTkLabel(f, text=t("登录口令"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=4)
        pw = ctk.CTkFrame(f, fg_color="transparent")
        pw.grid(row=r, column=1, sticky="ew", padx=14, pady=4)
        pw.grid_columnconfigure(0, weight=1)
        self.e_pass = ctk.CTkEntry(pw, font=theme.font(12), height=34, show="•",
                                   placeholder_text=t("登录口令"))
        self.e_pass.grid(row=0, column=0, sticky="ew")
        self.e_pass.insert(0, cfg.get_secret("password"))
        ctk.CTkButton(pw, text=t("重新登录"), width=80, height=34, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self._repair).grid(
            row=0, column=1, padx=(8, 0))
        r += 1

        ctk.CTkLabel(f, text="账号由管理员在面板「账号管理」里创建，没有自助注册。"
                             "登录后本机生成私钥，之后请求一律用私钥签名，口令不再传输；"
                             "服务器未开启鉴权时留空即可",
                     font=theme.font(10), text_color=c["muted"], wraplength=440,
                     justify="left").grid(row=r, column=1, sticky="w", padx=14)
        r += 1

        # ---- 启动时后台重扫
        # 有人觉得每次打开都重扫一遍是"卡"；也有人希望列表永远最新。
        # 这里给开关，默认开但完全静默（不阻塞界面），不需要就关掉。
        ctk.CTkLabel(f, text=t("启动时自动刷新"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=8)
        self.v_rescan = ctk.BooleanVar(value=bool(cfg.get("rescan_on_start", True)))
        ctk.CTkCheckBox(f, text=t("打开软件时在后台刷新一次实例列表"),
                        variable=self.v_rescan, font=theme.font(11),
                        fg_color=c["accent"], border_color=c["line_hi"],
                        text_color=c["text2"]).grid(
            row=r, column=1, sticky="w", padx=14, pady=8)
        r += 1
        ctk.CTkLabel(f, text=t("只扫上次命中的目录，不阻塞界面；嫌慢可关闭"),
                     font=theme.font(10), text_color=c["muted"],
                     justify="left").grid(row=r, column=1, sticky="w", padx=14)
        r += 1

        # ---- 实例扫描深度
        ctk.CTkLabel(f, text=t("实例扫描深度"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=8)
        self.e_depth = ctk.CTkEntry(f, font=theme.font(12), height=34, width=80)
        self.e_depth.grid(row=r, column=1, sticky="w", padx=14, pady=8)
        self.e_depth.insert(0, str(cfg.get("scan_depth", 3)))
        ctk.CTkLabel(f, text=t("越大越慢；找不到实例时可调到 4~5"), font=theme.font(10),
                     text_color=c["muted"]).grid(row=r, column=1, sticky="w", padx=110)
        r += 1

        # ---- 额外实例目录
        ctk.CTkLabel(f, text=t("额外实例目录"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="nw", padx=14, pady=8)
        wrap = ctk.CTkFrame(f, fg_color="transparent")
        wrap.grid(row=r, column=1, sticky="ew", padx=14, pady=8)
        self.root_box = ctk.CTkTextbox(wrap, height=72, font=theme.font(11))
        self.root_box.pack(fill="x")
        self.root_box.insert("1.0", "\n".join(cfg.get("custom_roots", []) or []))
        ctk.CTkButton(wrap, text=t("➕ 选择文件夹"), height=30, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self._pick_root).pack(anchor="w", pady=(6, 0))
        r += 1

        ctk.CTkLabel(f, text=t("一行一个目录，用于手动指定 PCL2 / HMCL 等绿色版所在位置"),
                     font=theme.font(10), text_color=c["muted"], wraplength=440,
                     justify="left").grid(row=r, column=1, sticky="w", padx=14, pady=(0, 8))


    def _pick_root(self):
        d = filedialog.askdirectory(title=t("选择启动器或实例所在目录"))
        if d:
            cur = self.root_box.get("1.0", "end").strip()
            self.root_box.delete("1.0", "end")
            self.root_box.insert("1.0", (cur + "\n" if cur else "") + d)

    # ------------------------------------------------------------ 同步
    def _build_sync(self, f):
        c = theme.colors()
        self.v_backup = ctk.BooleanVar(value=bool(cfg.get("backup_before_sync", True)))
        self.v_delremote = ctk.BooleanVar(value=bool(cfg.get("delete_remote_on_upload", True)))
        self.v_dellocal = ctk.BooleanVar(value=bool(cfg.get("delete_local_on_download", True)))
        self.v_core = ctk.BooleanVar(value=bool(cfg.get("core_only", False)))
        self.v_full = ctk.BooleanVar(value=bool(cfg.get("force_full_hash", False)))
        self.v_pd = ctk.BooleanVar(value=bool(cfg.get("auto_fix_playerdata", True)))

        opts = [
            ("同步前自动备份本地存档（硬链接，几乎不占空间）", self.v_backup),
            ("上传时：本地删除的文件，云端也删除", self.v_delremote),
            ("下载时：云端删除的文件，本地也删除", self.v_dellocal),
            ("只同步存档核心内容（level.dat / region / DIM / data / datapacks）", self.v_core),
            ("每次都重算哈希（关闭大小+时间预筛，最准但最慢）", self.v_full),
            (t("上传前自动摘除房主玩家数据（云端存档共享友好）"), self.v_pd),
        ]
        for i, (txt, var) in enumerate(opts):
            ctk.CTkSwitch(f, text=txt, variable=var, font=theme.font(12),
                          progress_color=c["accent"]).grid(row=i, column=0, sticky="w", padx=14, pady=10)

        r = len(opts)
        ctk.CTkLabel(f, text=t("备份保留份数"), font=theme.font(12)).grid(row=r, column=0, sticky="w", padx=14, pady=8)
        self.e_keep = ctk.CTkEntry(f, width=80, font=theme.font(12), height=32)
        self.e_keep.grid(row=r, column=0, sticky="w", padx=140, pady=8)
        self.e_keep.insert(0, str(cfg.get("keep_backups", 5)))

        ctk.CTkLabel(f, text=t("分块大小（MB）"), font=theme.font(12)).grid(row=r + 1, column=0, sticky="w", padx=14, pady=8)
        self.m_chunk = ctk.CTkOptionMenu(f, values=["1", "2", "4", "8", "16"], width=90, font=theme.font(12))
        self.m_chunk.grid(row=r + 1, column=0, sticky="w", padx=140, pady=8)
        self.m_chunk.set(str(cfg.get("chunk_size_mb", 4)))

        ctk.CTkLabel(f, text=t("并发线程数"), font=theme.font(12)).grid(row=r + 2, column=0, sticky="w", padx=14, pady=8)
        self.m_conc = ctk.CTkOptionMenu(f, values=["1", "2", "4", "6", "8", "12", "16"], width=90, font=theme.font(12))
        self.m_conc.grid(row=r + 2, column=0, sticky="w", padx=140, pady=8)
        self.m_conc.set(str(cfg.get("concurrency", 4)))

    # ------------------------------------------------------------ 忽略
    def _build_ignore(self, f):
        c = theme.colors()
        ctk.CTkLabel(f, text=t("在默认规则之外追加的规则（glob 语法，一行一条）"),
                     font=theme.font(12), wraplength=560, justify="left").pack(anchor="w", padx=14, pady=(12, 4))
        self.ig_box = ctk.CTkTextbox(f, font=theme.font(11), height=180)
        self.ig_box.pack(fill="both", expand=True, padx=14, pady=4)
        self.ig_box.insert("1.0", "\n".join(cfg.get("ignore_rules", []) or []))
        ctk.CTkLabel(f, text="默认已忽略：" + "、".join(DEFAULT_IGNORE_RULES[:8]) + " 等",
                     font=theme.font(10), text_color=c["muted"], wraplength=560, justify="left").pack(
            anchor="w", padx=14, pady=4)
        ctk.CTkLabel(f, text=t("支持 * ? 通配，目录以 / 结尾，! 开头表示强制包含"),
                     font=theme.font(10), text_color=c["muted"]).pack(anchor="w", padx=14, pady=(0, 10))

    # ------------------------------------------------------------ 高级
    def _build_adv(self, f):
        c = theme.colors()
        f.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(f, text=t("界面缩放"), font=theme.font(12)).grid(row=0, column=0, sticky="w", padx=14, pady=12)
        self.m_scale = ctk.CTkOptionMenu(f, values=["auto", "0.8", "0.9", "1.0", "1.1", "1.25", "1.4"], width=100,
                                         font=theme.font(12))
        self.m_scale.grid(row=0, column=1, sticky="w", padx=14, pady=12)
        self.m_scale.set("auto" if cfg.get("auto_scale", True) else str(cfg.get("ui_scale", 1.0)))

        ctk.CTkLabel(f, text=t("外观点"), font=theme.font(12)).grid(row=1, column=0, sticky="w", padx=14, pady=12)
        self.m_theme = ctk.CTkOptionMenu(f, values=["深色", "浅色", "跟随系统"], width=120, font=theme.font(12))
        self.m_theme.grid(row=1, column=1, sticky="w", padx=14, pady=12)
        self.m_theme.set({"dark": "深色", "light": "浅色", "system": "跟随系统"}.get(cfg.get("theme_mode", "dark"), "深色"))

        ctk.CTkLabel(f, text=t("界面语言"), font=theme.font(12)).grid(row=1, column=0, sticky="w", padx=(180, 14), pady=12)
        self.m_lang = ctk.CTkOptionMenu(f, values=list(i18n.LANGUAGES.values()), width=120, font=theme.font(12))
        self.m_lang.grid(row=1, column=1, sticky="w", padx=(180, 14), pady=12)
        _cur = str(cfg.get("language", "zh") or "zh")
        self.m_lang.set(i18n.LANGUAGES.get(_cur, i18n.LANGUAGES["zh"]))

        ctk.CTkLabel(f, text=t("本地缓存"), font=theme.font(12)).grid(row=2, column=0, sticky="w", padx=14, pady=12)
        btns = ctk.CTkFrame(f, fg_color="transparent")
        btns.grid(row=2, column=1, sticky="w", padx=14, pady=12)
        ctk.CTkButton(btns, text=t("清理块缓存"), width=110, height=30, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"], text_color=c["text"],
                      command=self._clear_cache).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text=t("打开配置目录"), width=110, height=30, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"], text_color=c["text"],
                      command=self._open_dir).pack(side="left")
        ctk.CTkLabel(f, text=f"配置与缓存目录：{cfg.dir}", font=theme.font(10),
                     text_color=c["muted"], wraplength=480, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=14, pady=4)

    def _clear_cache(self):
        d = os.path.join(cfg.dir, "blobcache")
        n = 0
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                    n += 1
                except OSError:
                    pass
        self._toast(f"已清理 {n} 个缓存块")

    def _open_dir(self):
        try:
            os.startfile(cfg.dir)  # noqa: S606
        except Exception:
            pass

    # ------------------------------------------------------------ 关于
    def _build_about(self, f):
        c = theme.colors()
        ctk.CTkLabel(f, text="☁ MCSync", font=theme.font(22, "bold")).pack(anchor="w", padx=16, pady=(18, 4))
        ctk.CTkLabel(f, text=t("Minecraft 存档云同步工具 v1.0"), font=theme.font(12),
                     text_color=c["muted"]).pack(anchor="w", padx=16)
        lines = [
            "• 自动识别 PCL2 / HMCL / 官方 / Prism / MultiMC / Modrinth 实例与存档",
            "• 内容寻址分块（4MiB）：只传变化的部分，重复数据秒传",
            "• 哈希增量比对：大小+修改时间预筛，xxHash 快速哈希",
            "• 版本快照：每次同步生成一个版本，可随时回滚",
            "• 冲突检测：本地与云端同时修改时弹窗选择处理方式",
            "",
            "服务端：Linux 一键部署（Docker / systemd 双模式）",
            "Web 管理页：浏览器打开服务端地址即可管理云端存档",
        ]
        for _line in lines:
            ctk.CTkLabel(f, text=_line, font=theme.font(11), anchor="w", justify="left",
                         wraplength=560).pack(anchor="w", padx=16, pady=2)

    # ------------------------------------------------------------ 保存
    def _repair(self):
        """用账号口令重新登录（丢弃旧私钥，重新登记）。"""
        from core.api import Api
        cfg.set("server_url", self.e_url.get().strip())
        cfg.set("username", self.e_user.get().strip())
        cfg.set_secret(self.e_pass.get().strip(), "password")
        cfg.set_secret("")
        cfg.set("paired_server", "")
        try:
            api = Api(cfg.server_url)
            api.login(cfg.get("username", ""), cfg.get_secret("password"))
            who = api.me or {}
            role = "管理员" if who.get("role") == "admin" else "普通用户"
            self._toast(f"登录成功：{who.get('display') or cfg.get('username','')}（{role}）")
        except Exception as e:
            self._toast(f"登录失败：{e}")

    def _toast(self, msg: str):
        try:
            self.master_window.toast(msg)
        except Exception:
            pass

    def _save(self):
        cfg.set("server_url", self.e_url.get().strip())
        # 账号口令必须最先落盘：后面解析数值时若出异常（如控件取到空值），
        # 也不至于把已经填好的登录信息一起丢掉。
        cfg.set("username", self.e_user.get().strip())
        cfg.set_secret(self.e_pass.get().strip(), "password")
        try:
            cfg.set("scan_depth", max(1, min(6, int(self.e_depth.get().strip() or "3"))))
        except ValueError:
            pass
        # 启动刷新开关单独 try：控件缺失或取值异常都不影响其余设置落盘
        try:
            cfg.set("rescan_on_start", bool(self.v_rescan.get()))
        except Exception:
            pass
        roots = [r.strip() for r in self.root_box.get("1.0", "end").splitlines() if r.strip()]
        cfg.set("custom_roots", roots)
        cfg.set("auto_sync_enabled", bool(self.v_auto.get()))
        try:
            cfg.set("auto_sync_minutes", max(5, int(self.e_auto.get().strip() or "30")))
        except ValueError:
            pass
        cfg.set("allow_sync_while_running", bool(self.v_running.get()))
        cfg.set("backup_before_sync", bool(self.v_backup.get()))
        cfg.set("delete_remote_on_upload", bool(self.v_delremote.get()))
        cfg.set("delete_local_on_download", bool(self.v_dellocal.get()))
        cfg.set("core_only", bool(self.v_core.get()))
        cfg.set("force_full_hash", bool(self.v_full.get()))
        try:
            cfg.set("auto_fix_playerdata", bool(self.v_pd.get()))
        except Exception:
            pass
        try:
            cfg.set("keep_backups", max(0, int(self.e_keep.get().strip() or "5")))
        except ValueError:
            pass
        # 这两个来自下拉框，取到异常值不该让整个保存中断
        try:
            cfg.set("chunk_size_mb", int(str(self.m_chunk.get()).strip()))
        except (ValueError, TypeError):
            pass
        try:
            cfg.set("concurrency", int(str(self.m_conc.get()).strip()))
        except (ValueError, TypeError):
            pass
        rules = [r.strip() for r in self.ig_box.get("1.0", "end").splitlines() if r.strip()]
        cfg.set("ignore_rules", rules)
        sc = self.m_scale.get()
        if sc == "auto":
            cfg.set("auto_scale", True)
        else:
            try:
                cfg.set("auto_scale", False)
                cfg.set("ui_scale", float(sc))
            except (ValueError, TypeError):
                cfg.set("auto_scale", True)
        try:
            cfg.set("theme_mode", {"深色": "dark", "浅色": "light",
                                   "跟随系统": "system"}[self.m_theme.get()])
        except KeyError:
            pass
        rev = {v: k for k, v in i18n.LANGUAGES.items()}
        cfg.set("language", rev.get(str(self.m_lang.get()), "zh"))
        if self.on_saved:
            self.on_saved()
        self._close()


def _noop():
    pass


# ============================================================================
#  通用确认 / 输入
# ============================================================================
class ConfirmDialog(_BaseDialog):
    def __init__(self, master, title: str, message: str, ok_text: str = "确定",
                 danger: bool = False, extra: Optional[str] = None):
        super().__init__(master, title, 520, 260)
        c = theme.colors()
        ctk.CTkLabel(self, text=message, font=theme.font(13), wraplength=460, justify="left").pack(
            anchor="w", padx=20, pady=(24, 8))
        if extra:
            ctk.CTkLabel(self, text=extra, font=theme.font(11), text_color=c["muted"], wraplength=460,
                         justify="left").pack(anchor="w", padx=20, pady=(0, 8))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"], font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=ok_text, width=110, height=34, font=theme.font(12, "bold"),
                      fg_color=c["danger"] if danger else None, hover_color="#b91c1c" if danger else None,
                      command=self._ok).pack(side="right")
        self.wait_window()

    def _ok(self):
        self.result = True
        self._close()


class _InstancePickDialog(_BaseDialog):
    """选择云端存档所属整合包（可下拉选已有实例，也可直接输入新名字）。"""

    def __init__(self, master, save_name: str, current: str, choices: List[str]):
        super().__init__(master, "归类到整合包", 480, 300)
        self.result: Optional[str] = None
        c = theme.colors()

        ctk.CTkLabel(self, text=f"云端存档：{save_name}", font=theme.font(12, "bold"),
                     wraplength=420, justify="left", anchor="w").pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(self, text=t("选择它所属的整合包（实例）。留空表示归入「未分类」。"),
                     font=theme.font(10), text_color=c["muted"], wraplength=420,
                     justify="left", anchor="w").pack(anchor="w", padx=20, pady=(0, 12))

        opts = ["（留空 · 未分类）"] + [x for x in choices if x]
        self.m = ctk.CTkOptionMenu(self, values=opts, font=theme.font(12), height=34,
                                   fg_color=c["card"], button_color=c["accent"],
                                   button_hover_color=c["accent_hover"])
        self.m.pack(fill="x", padx=20)
        self.m.set(current if current and current in opts else opts[0])

        ctk.CTkLabel(self, text=t("或直接输入整合包名："), font=theme.font(10),
                     text_color=c["muted"], anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        self.e = ctk.CTkEntry(self, font=theme.font(12), height=34,
                              placeholder_text=t("留空则使用上面的下拉选择"))
        self.e.pack(fill="x", padx=20)
        self.e.bind("<Return>", lambda ev: self._ok())

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent",
                      border_width=1, border_color=c["line"], text_color=c["text"],
                      font=theme.font(12), command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("确定"), width=100, height=34, font=theme.font(12, "bold"),
                      command=self._ok).pack(side="right")
        self.wait_window()

    def _ok(self):
        typed = (self.e.get() or "").strip()
        if typed:
            self.result = typed
        else:
            sel = self.m.get()
            self.result = "" if sel.startswith("（留空") else sel
        self._close()


class _DownloadTargetDialog(_BaseDialog):
    """选择云端存档下载到本地的落点。

    规则：
      1) 云端存档带有所属整合包（实例）名。本机若有同名实例，
         默认落到该实例的存档目录 —— 于是 A 在某个整合包里上传的存档，
         B 下载时会自动装到 B 本机同名整合包下，不会串位置。
      2) 本机没有同名实例时，必须手动选择实例或文件夹。
    """

    def __init__(self, master, save_name: str, instance: str,
                 instance_names: List[str], instance_dirs: dict,
                 default_dir: str = ""):
        super().__init__(master, "下载到哪个实例", 560, 400)
        self.result: Optional[str] = None
        c = theme.colors()
        self.instance_dirs = instance_dirs or {}
        self.default_dir = default_dir or ""

        ctk.CTkLabel(self, text=f"云端存档：{save_name}", font=theme.font(13, "bold"),
                     anchor="w", wraplength=500).pack(anchor="w", padx=20, pady=(18, 2))

        if instance and default_dir:
            tip = (f"它来自整合包「{instance}」，本机已找到同名实例，"
                   f"默认装到该实例的存档目录。")
        elif instance:
            tip = (f"它来自整合包「{instance}」，但本机没有同名实例。"
                   f"请手动选择要装到哪个实例的存档目录。")
        else:
            tip = "该云端存档没有记录整合包信息，请手动选择保存位置。"
        ctk.CTkLabel(self, text=tip, font=theme.font(10), text_color=c["muted"],
                     anchor="w", wraplength=500, justify="left").pack(
            anchor="w", padx=20, pady=(0, 12))

        if instance_names:
            ctk.CTkLabel(self, text=t("装到这个实例："), font=theme.font(11),
                         anchor="w").pack(anchor="w", padx=20, pady=(0, 4))
            self.m = ctk.CTkOptionMenu(
                self, values=instance_names, font=theme.font(12), height=34,
                fg_color=c["card"], button_color=c["accent"],
                button_hover_color=c["accent_hover"], command=lambda _v: self._sync())
            self.m.pack(fill="x", padx=20)
            if instance in instance_names:
                self.m.set(instance)

        ctk.CTkLabel(self, text=t("保存为（存档文件夹名）："), font=theme.font(11),
                     anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        self.e_name = ctk.CTkEntry(self, font=theme.font(12), height=34)
        self.e_name.pack(fill="x", padx=20)
        self.e_name.insert(0, save_name)

        ctk.CTkLabel(self, text=t("完整路径："), font=theme.font(11),
                     anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20)
        row.grid_columnconfigure(0, weight=1)
        self.e_dir = ctk.CTkEntry(row, font=theme.font(11), height=34)
        self.e_dir.grid(row=0, column=0, sticky="ew")
        self.e_dir.insert(0, self.default_dir)
        ctk.CTkButton(row, text=t("浏览"), width=70, height=34, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self._pick).grid(
            row=0, column=1, padx=(8, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent",
                      border_width=1, border_color=c["line"], text_color=c["text"],
                      font=theme.font(12), command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("开始下载"), width=110, height=34,
                      font=theme.font(12, "bold"), command=self._ok).pack(side="right")
        self.wait_window()

    def _save_name(self) -> str:
        return (self.e_name.get() or "").strip() or "world"

    def _sync(self):
        """切换实例时刷新目标路径"""
        base = self.instance_dirs.get(self.m.get(), "")
        if base:
            self.e_dir.delete(0, "end")
            self.e_dir.insert(0, os.path.join(base, self._save_name()))

    def _pick(self):
        d = filedialog.askdirectory(title=t("选择保存位置"))
        if d:
            self.e_dir.delete(0, "end")
            self.e_dir.insert(0, os.path.join(d, self._save_name()))

    def _ok(self):
        p = (self.e_dir.get() or "").strip()
        if not p:
            return
        self.result = p
        self._close()


class InputDialog(_BaseDialog):
    def __init__(self, master, title: str, label: str, default: str = ""):
        super().__init__(master, title, 460, 220)
        c = theme.colors()
        ctk.CTkLabel(self, text=label, font=theme.font(12), wraplength=400, justify="left").pack(
            anchor="w", padx=20, pady=(22, 6))
        self.e = ctk.CTkEntry(self, font=theme.font(12), height=34)
        self.e.pack(fill="x", padx=20)
        self.e.insert(0, default)
        self.e.focus()
        self.e.bind("<Return>", lambda e: self._ok())
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"], font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("确定"), width=100, height=34, font=theme.font(12, "bold"),
                      command=self._ok).pack(side="right")
        self.wait_window()

    def _ok(self):
        self.result = self.e.get().strip()
        self._close()

        # ---- 定时同步（默认关闭）
        ctk.CTkLabel(f, text=t("定时同步"), font=theme.font(12, "bold")).grid(
            row=r, column=0, sticky="w", padx=14, pady=(8, 4))
        asf = ctk.CTkFrame(f, fg_color="transparent")
        asf.grid(row=r, column=1, sticky="ew", padx=14, pady=(8, 4))
        self.v_auto = ctk.BooleanVar(value=bool(cfg.get("auto_sync_enabled", False)))
        ctk.CTkSwitch(asf, text=t("开启（默认关闭）"), variable=self.v_auto,
                      font=theme.font(12), progress_color=c["accent"]).pack(side="left")
        self.e_auto = ctk.CTkEntry(asf, font=theme.font(12), width=60, height=30)
        self.e_auto.pack(side="left", padx=(10, 0))
        self.e_auto.insert(0, str(cfg.get("auto_sync_minutes", 30)))
        ctk.CTkLabel(asf, text=t("分钟一次（最少 5）"), font=theme.font(10),
                     text_color=c["muted"]).pack(side="left", padx=(6, 0))
        r += 1
        ctk.CTkLabel(f, text=t("开启后按间隔自动上传当前选中的存档；关闭窗口即停止。"),
                     font=theme.font(10), text_color=c["muted"], wraplength=440,
                     justify="left").grid(row=r, column=1, sticky="w", padx=14)
        r += 1

        self.v_running = ctk.BooleanVar(value=bool(cfg.get("allow_sync_while_running", False)))
        ctk.CTkSwitch(f, text=t("允许在游戏运行时同步（可能读到半截数据，不建议）"),
                      variable=self.v_running, font=theme.font(12),
                      progress_color=c["accent"]).grid(
            row=r, column=1, sticky="w", padx=14, pady=(4, 6))
        r += 1
