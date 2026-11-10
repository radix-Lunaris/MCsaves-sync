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

"""MCSync 客户端主窗口"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from core.api import Api, MCSyncError, AuthError
from core.config import cfg
from core.scanner import InstanceScanner
from core.models import Instance, SaveInfo
from core.protocol import human_eta, human_size, human_speed
from core.sync import SyncEngine
from core.transfer import CancelToken, Progress
from ui import theme
from ui import design as ds
from ui.dialogs import (ConfirmDialog, ConflictDialog, SettingsDialog, VersionDialog,
                        _InstancePickDialog, _DownloadTargetDialog)
from ui.widgets import CloudCard, InstanceCard, SaveCard
from core.i18n import t


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        # 先隐藏再布局：Tk 窗口创建后立刻就会以默认尺寸（很小）显示出来，
        # 之后才被 minsize / geometry 逐级放大，用户看到的就是
        # "先一个小窗口，然后突然撑大"。这里全程隐藏，布局完成后再显示。
        self.withdraw()
        self.cfg = cfg
        theme.enable_dpi_awareness()
        mode = self.cfg.get("theme_mode", "dark")
        theme.apply(mode, 1.0)

        self.title("MCSync · Minecraft 存档云同步")
        self._set_icon()

        # ---- 缩放自适应
        scale = 1.0
        if self.cfg.get("auto_scale", True):
            scale = theme.auto_scale(self)
            self.cfg.set("ui_scale", scale, autosave=False)
        else:
            scale = float(self.cfg.get("ui_scale", 1.0) or 1.0)
        theme.apply(mode, scale)

        geo = self.cfg.get("window_geometry", "") or ""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 1200, 780
        self.minsize(880, 600)
        if geo and geo.count("x") >= 1 and "+" in geo:
            try:
                self.geometry(geo)
            except Exception:
                self.geometry(f"{w}x{h}")
        else:
            self.geometry(f"{min(w, int(sw*0.82))}x{min(h, int(sh*0.85))}")
        self.update_idletasks()
        if not geo:
            self.geometry(f"+{max(0,(sw-w)//2)}+{max(0,(sh-h)//6)}")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- 状态
        self.api = Api(self.cfg.server_url)
        # 日志缓冲必须在任何 log() 之前就绪。
        # 旧版本把 _prepare_api() 放在 _build_ui() 之前，而 _log_buf 由 _build_log_page 创建，
        # 结果配对阶段一旦写日志就会 AttributeError（真实环境直接崩）。
        self._log_buf: List[str] = []
        self._log_rendered = 0
        self._log_visible = False
        self.instances: List[Instance] = []
        self.saves: List[SaveInfo] = []
        self.cloud: List[dict] = []
        self.cur_instance: Optional[Instance] = None
        self.cur_save: Optional[SaveInfo] = None
        self.cur_cloud: Optional[dict] = None
        self.busy = False
        self._cancel = CancelToken()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcsync")
        self._ui_queue: queue.Queue = queue.Queue()
        self._progress = Progress(cb=self._on_progress)

        self._build_ui()
        # 配对放在界面就绪之后，早期日志才不会丢
        self._prepare_api()
        # 自检：UI 回调引用的方法必须真实存在。
        # 历史上曾因重构删掉了按钮绑定的方法，导致启动时 AttributeError 崩溃，
        # 这里在构建完成后立刻校验，把问题暴露成一条可读的提示而不是崩溃栈。
        try:
            missing = self._missing_callbacks()
            if missing:
                self.log("警告：以下界面功能未正确接入，相关按钮不可用："
                         + "、".join(missing))
        except Exception:
            pass
        # 这里不 deiconify：显示时机交给 main()，登录流程结束后再统一显示。
        # 若在这里就显示，会出现"主窗口先冒出来、登录框再叠上去"的跳变。
        self.after(120, self._boot)

    def _missing_callbacks(self) -> List[str]:
        """扫描本类源码，找出按钮回调引用了但未定义的方法名。"""
        import inspect
        import re as _re
        try:
            src = inspect.getsource(type(self))
        except Exception:
            return []
        defined = set(_re.findall(r"^    def (\w+)", src, _re.M))
        refs = set(_re.findall(r"command=(?:lambda[^:]*:)?\s*self\.(\w+)", src))
        refs |= set(_re.findall(r"self\.after\(\s*\d+,\s*self\.(\w+)", src))
        refs |= set(_re.findall(r"command=lambda[^,]*?self\.(\w+)\(", src))
        return sorted(r for r in refs if r not in defined)

    # ================================================================== UI
    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "app.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

    def _build_ui(self):
        c = theme.colors()
        self.configure(fg_color=c["card"])
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_topbar().grid(row=0, column=0, sticky="ew")
        self._build_main().grid(row=1, column=0, sticky="nsew")
        self._build_bottom().grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------ 顶栏
    # 单行三区：左=品牌，中=连接状态，右=身份/登录 + 外观 + 设置。
    #
    # 为什么不再内联登录表单：
    #   登录已独立成启动窗口（ui/login_window.py），打开软件第一屏就是它。
    #   顶栏继续挂三个输入框，登录完成后它们就没用了，却还占着一整行，
    #   属于"用完不收"的版面浪费。现在只保留一个状态胶囊 + 登录入口。
    #
    # 间距全部取自 design.SP（4/8/12/16/24/32），不再随手填数值。
    def _build_topbar(self) -> ctk.CTkFrame:
        c = theme.colors()
        sp, fs, rd, sz = ds.SP, ds.FS, ds.RD, ds.SZ
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=c["surface"],
                           border_width=0)
        bar.grid_columnconfigure(1, weight=1)

        # ---- 左：品牌
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=sp["m"], pady=sp["s"])
        ctk.CTkLabel(left, text="☁", font=theme.font(18),
                     text_color=c["accent"]).pack(side="left", padx=(0, sp["xs"]))
        ctk.CTkLabel(left, text="MCSync", font=theme.font(fs["h2"], "bold"),
                     text_color=c["text"]).pack(side="left")

        # ---- 中：连接状态（次要信息，不抢主视觉）
        self.l_status = ctk.CTkLabel(bar, text=t("● 未连接"),
                                     font=theme.font(fs["cap"]),
                                     text_color=c["muted"])
        self.l_status.grid(row=0, column=1, sticky="w", padx=sp["xs"])

        # ---- 右：身份 + 操作
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=sp["m"], pady=sp["s"])

        # 身份胶囊：登录后才显示
        self.login_chip = ctk.CTkFrame(right, fg_color=c["sel_bg"],
                                       corner_radius=rd["pill"])
        self.l_user = ctk.CTkLabel(self.login_chip, text="",
                                   font=theme.font(fs["cap"]),
                                   text_color=c["text"])
        self.l_user.pack(side="left", padx=(sp["s"], sp["xxs"]))
        self.b_logout = ctk.CTkButton(self.login_chip, text="退出", width=44,
                                      height=22, font=theme.font(fs["cap"]),
                                      fg_color="transparent",
                                      text_color=c["text2"],
                                      hover_color=c["surface3"],
                                      command=self.do_logout)
        self.b_logout.pack(side="left", padx=(0, sp["xxs"]))

        self.b_login = ctk.CTkButton(right, text=t("登录"), width=64,
                                     height=sz["btn_h_sm"],
                                     font=theme.font(fs["sub"]),
                                     fg_color=c["accent"],
                                     hover_color=c["accent_h"],
                                     text_color=c["accent_fg"],
                                     corner_radius=rd["ctl"],
                                     command=self.open_login)
        self.b_login.pack(side="left", padx=(0, sp["xs"]))

        self.b_theme = ctk.CTkButton(right, text="🌙", width=sz["btn_h_sm"],
                                     height=sz["btn_h_sm"],
                                     font=theme.font(fs["sub"]),
                                     fg_color="transparent", border_width=1,
                                     border_color=c["line"],
                                     text_color=c["text"],
                                     command=self.toggle_theme)
        self.b_theme.pack(side="left", padx=(0, sp["xs"]))
        ctk.CTkButton(right, text="⚙", width=sz["btn_h_sm"],
                      height=sz["btn_h_sm"], font=theme.font(fs["sub"]),
                      fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"],
                      command=self.open_settings).pack(side="left")

        self._refresh_login_row()
        return bar

    def _refresh_login_row(self):
        """按登录状态切换顶栏右侧：已登录显示身份胶囊，未登录显示登录按钮。

        渐进式披露：登录成功后按钮就退场，不留无用控件占用注意力。
        """
        try:
            logged = bool(getattr(self.api, "client_secret", "")) and bool(
                self.cfg.get("username", ""))
        except Exception:
            logged = False
        if logged:
            who = getattr(self.api, "me", {}) or {}
            role = t("管理员") if who.get("role") == "admin" else t("普通用户")
            name = who.get("display") or self.cfg.get("username", "")
            self.l_user.configure(text=f"✓ {name} · {role}")
            self.b_login.pack_forget()
            self.login_chip.pack(side="left", padx=(0, ds.SP["xs"]))
        else:
            self.login_chip.pack_forget()
            self.b_login.pack(side="left", padx=(0, ds.SP["xs"]))

    def open_login(self):
        """打开登录窗口（顶栏入口 / 手动重新登录）。"""
        try:
            from ui.login_window import LoginWindow
            LoginWindow(on_success=self.on_login_result, auto=False)
        except Exception as e:
            self.log(f"打开登录窗口失败：{e}")
            self.toast(str(e))

    def on_login_result(self, res):
        """登录窗口回调：成功则接管连接并刷新云端。"""
        if not res:
            # 用户选择"稍后"或直接关闭：仍可离线使用本地功能
            self._refresh_login_row()
            return
        try:
            self.api = res.get("api") or Api(self.cfg.get("server_url", ""))
            self._prepare_api()
            self._refresh_login_row()
            self.toast(t("登录成功"))
            self.refresh_cloud()
            self.test_connection(silent=True)
        except Exception as e:
            self.log(f"登录后初始化失败：{e}")
            self.toast(str(e))

    def do_logout(self):
        """退出登录：清账号与私钥，回到未登录态。"""
        self.cfg.set("paired_server", "")
        try:
            self.cfg.set_secret("")
        except Exception:
            self.cfg.set("client_secret", "")
        who = self.cfg.get("username", "")
        self.cfg.set("username", "")
        self.cfg.set_secret("", "password")
        self.cfg.set("remember_password", False)
        self.api = Api(self.cfg.get("server_url", ""))
        self._refresh_login_row()
        self.l_status.configure(text=t("● 未连接"),
                                text_color=theme.colors()["muted"])
        self.log(f"已退出登录（{who}）")

    # ------------------------------------------------------------ 主区
    # 布局：左侧竖向选项卡 + 右侧内容区（一次只显示一页）
    # 旧版是三栏并排，窄屏下三列互相挤压导致显示不全，改为选项卡后内容区始终占满。
    def _build_main(self) -> ctk.CTkFrame:
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=0)     # 侧栏宽度固定
        main.grid_columnconfigure(1, weight=1)     # 内容区占满剩余空间

        self.sidebar = self._build_sidebar(main)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 6))

        content = ctk.CTkFrame(main, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=(8, 10), pady=(10, 6))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.pages: Dict[str, ctk.CTkFrame] = {}

        # ---- 页1：本地（左=实例列，右=存档区）
        # 递进关系用左右分栏表达比上下更直观：选左边实例，右边出存档。
        pg = ctk.CTkFrame(content, fg_color="transparent")
        pg.grid(row=0, column=0, sticky="nsew")
        pg.grid_rowconfigure(0, weight=1)
        pg.grid_columnconfigure(0, weight=0, minsize=232)
        pg.grid_columnconfigure(1, weight=1)
        self.left = self._build_left(pg)
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.center = self._build_center(pg)
        self.center.grid(row=0, column=1, sticky="nsew")
        self.pages["local"] = pg

        # ---- 页2：云端
        pg2 = ctk.CTkFrame(content, fg_color="transparent")
        pg2.grid(row=0, column=0, sticky="nsew")
        pg2.grid_rowconfigure(0, weight=1)
        pg2.grid_columnconfigure(0, weight=1)
        self.right = self._build_right(pg2)
        self.right.grid(row=0, column=0, sticky="nsew")
        self.pages["cloud"] = pg2

        # ---- 页3：日志
        pg3 = ctk.CTkFrame(content, fg_color="transparent")
        pg3.grid(row=0, column=0, sticky="nsew")
        self._build_log_page(pg3)
        self.pages["log"] = pg3

        # ---- 页4：区块清理（按 InhabitedTime 筛选，默认只预览）
        pg4 = ctk.CTkFrame(content, fg_color="transparent")
        pg4.grid(row=0, column=0, sticky="nsew")
        pg4.grid_rowconfigure(0, weight=1)
        pg4.grid_columnconfigure(0, weight=1)
        from ui.prune_page import PrunePage
        self.prune_page = PrunePage(pg4, self)
        self.prune_page.grid(row=0, column=0, sticky="nsew")
        self.pages["prune"] = pg4

        # ---- 页5：玩家数据（解除共享存档里的房主角色数据）
        pg5 = ctk.CTkFrame(content, fg_color="transparent")
        pg5.grid(row=0, column=0, sticky="nsew")
        pg5.grid_rowconfigure(0, weight=1)
        pg5.grid_columnconfigure(0, weight=1)
        from ui.playerdata_page import PlayerDataPage
        self.playerdata_page = PlayerDataPage(pg5, self)
        self.playerdata_page.grid(row=0, column=0, sticky="nsew")
        self.pages["playerdata"] = pg5

        self.cur_page = "local"
        self.show_page("local")
        return main

    # ------------------------------------------------------------ 侧栏
    def _build_sidebar(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        sp, fs, rd, sz = ds.SP, ds.FS, ds.RD, ds.SZ
        bar = ctk.CTkFrame(master, width=sz["sidebar"], corner_radius=rd["card"],
                           fg_color=c["surface"], border_width=1, border_color=c["line"])
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_rowconfigure(9, weight=1)

        # 顶栏已经有品牌标识了，侧栏再放一次是重复信息 —— 直接给导航留空间。
        ctk.CTkLabel(bar, text=t("导航"), font=theme.font(fs["cap"]),
                     text_color=c["muted"]).grid(
            row=0, column=0, sticky="w", padx=sp["m"], pady=(sp["m"], sp["xs"]))

        self.tab_btns: Dict[str, ctk.CTkButton] = {}
        self.tabs = [("local", "📦", "本地存档"), ("cloud", "☁", "云端存档"),
                     ("prune", "🧹", "区块清理"), ("playerdata", "🪪", "玩家数据"),
                     ("log", "📜", "传输日志")]
        for i, (key, icon, label) in enumerate(self.tabs):
            b = ctk.CTkButton(
                bar, text=f"  {icon}  {label}", anchor="w", height=42,
                font=theme.font(fs["body"], "bold"), corner_radius=rd["ctl"],
                fg_color="transparent", text_color=c["text2"],
                hover_color=c["surface3"],
                command=lambda k=key: self.show_page(k))
            b.grid(row=1 + i, column=0, sticky="ew", padx=sp["xs"], pady=(0, sp["xxs"]))
            self.tab_btns[key] = b

        # 底部快捷入口
        ctk.CTkButton(bar, text=t("  ⚙  设置"), anchor="w", height=36,
                      font=theme.font(fs["sub"]),
                      fg_color="transparent", text_color=c["muted"],
                      hover_color=c["surface3"],
                      command=self.open_settings).grid(
            row=10, column=0, sticky="ew", padx=sp["xs"], pady=(0, sp["s"]))
        return bar

    def show_page(self, key: str):
        self.cur_page = key
        pg = self.pages[key]
        # tkraise 在某些桩件/老版本上不可用，降级处理不影响功能
        try:
            pg.tkraise()
        except Exception:
            try:
                pg.lift()
            except Exception:
                pass
        self._log_visible = (key == "log")
        c = theme.colors()
        for k, b in self.tab_btns.items():
            if k == key:
                # 选中态：用"低饱和底色 + 强调左边条"而非整块高饱和填充。
                # 高饱和整块会与内容区抢注意力，而且三个页签都填满时看不出主次。
                b.configure(fg_color=c["sel_bg"], text_color=c["accent"],
                            hover_color=c["sel_bg"])
            else:
                b.configure(fg_color="transparent", text_color=c["text2"],
                            hover_color=c["surface3"])
        # 切到日志页时立刻补齐尚未渲染的内容
        if key == "log":
            self.after(30, self._flush_log)
        # 切到清理页时把当前实例/存档带过去，不用再选一遍
        if key == "prune" and hasattr(self, "prune_page"):
            try:
                self.prune_page.on_show()
            except Exception:
                pass
        if key == "playerdata" and hasattr(self, "playerdata_page"):
            try:
                self.playerdata_page.on_show()
            except Exception:
                pass

    def _panel(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        f = ctk.CTkFrame(master, corner_radius=14, fg_color=c["card2"], border_width=1, border_color=c["line"])
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)
        return f

    def _build_left(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        p = self._panel(master)
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 4))
        ctk.CTkLabel(head, text=t("游戏实例"), font=theme.font(14, "bold")).pack(side="left", padx=6)
        ctk.CTkButton(head, text=t("🔍 全盘重扫"), width=104, height=28, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.rescan_full).pack(side="right", padx=2)
        ctk.CTkButton(head, text=t("⟳ 实例重扫"), width=98, height=28, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.rescan_known).pack(side="right", padx=2)
        ctk.CTkButton(head, text="➕", width=30, height=28, font=theme.font(12),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.add_instance_dir).pack(side="right", padx=2)

        self.inst_box = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.inst_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self.inst_box.grid_columnconfigure(0, weight=1)

        self.l_inst_count = ctk.CTkLabel(p, text="", font=theme.font(10), text_color=c["muted"])
        self.l_inst_count.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 8))
        return p

    def _build_center(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        p = self._panel(master)
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 4))
        head.grid_columnconfigure(0, weight=1)
        self.l_save_title = ctk.CTkLabel(head, text=t("存档"), font=theme.font(14, "bold"))
        self.l_save_title.grid(row=0, column=0, sticky="w", padx=6)
        self.b_scan_saves = ctk.CTkButton(head, text=t("⟳ 刷新"), width=64, height=28, font=theme.font(11),
                                          fg_color="transparent", border_width=1, border_color=c["line"],
                                          text_color=c["text"], command=lambda: self.load_saves(self.cur_instance))
        self.b_scan_saves.grid(row=0, column=1, padx=2)
        ctk.CTkButton(head, text=t("📂 打开目录"), width=92, height=28, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.open_save_dir).grid(row=0, column=2, padx=2)

        self.save_box = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.save_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self.save_box.grid_columnconfigure(0, weight=1)

        # 操作栏
        act = ctk.CTkFrame(p, fg_color="transparent")
        act.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 10))
        act.grid_columnconfigure(0, weight=1)
        act.grid_columnconfigure(1, weight=1)
        self.b_upload = ctk.CTkButton(act, text=t("⬆  上传到云端"), height=40, font=theme.font(13, "bold"),
                                      command=self.do_upload)
        self.b_upload.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.b_download = ctk.CTkButton(act, text=t("⬇  下载到本地"), height=40, font=theme.font(13, "bold"),
                                        fg_color=c["card"], border_width=1, border_color=c["accent"],
                                        text_color=c["accent"], hover_color=c["card2"],
                                        command=self.do_download)
        self.b_download.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.b_cfg = ctk.CTkButton(act, text=t("⚙  同步整合包配置"), height=30, font=theme.font(11),
                                   fg_color="transparent", border_width=1,
                                   border_color=c["line"], text_color=c["text"],
                                   command=self.do_upload_config)
        self.b_cfg.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=(6, 0))
        self.l_sel = ctk.CTkLabel(p, text=t("未选择存档"), font=theme.font(10), text_color=c["muted"],
                                  wraplength=460, justify="left", anchor="w")
        self.l_sel.grid(row=3, column=0, sticky="w", padx=14, pady=(0, 8))
        return p

    def _build_right(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        p = self._panel(master)
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 4))
        ctk.CTkLabel(head, text=t("云端存档"), font=theme.font(14, "bold")).pack(side="left", padx=6)
        ctk.CTkButton(head, text=t("🏷 归类"), width=66, height=28, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.assign_instance).pack(side="right", padx=2)
        ctk.CTkButton(head, text="⟳", width=30, height=28, font=theme.font(12),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.refresh_cloud).pack(side="right", padx=2)
        ctk.CTkButton(head, text="🌐", width=30, height=28, font=theme.font(12),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.open_web).pack(side="right", padx=2)

        self.cloud_box = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.cloud_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self.cloud_box.grid_columnconfigure(0, weight=1)

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 4))
        act.grid_columnconfigure(0, weight=1)
        act.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(act, text=t("版本管理"), height=32, font=theme.font(12),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self.manage_versions).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(act, text=t("删除云端"), height=32, font=theme.font(12),
                      fg_color="transparent", border_width=1, border_color=c["danger"],
                      text_color=c["danger"], hover_color="#7f1d1d", command=self.delete_cloud).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        self.l_cloud = ctk.CTkLabel(p, text=t("未连接"), font=theme.font(10), text_color=c["muted"],
                                    wraplength=280, justify="left", anchor="w")
        self.l_cloud.grid(row=3, column=0, sticky="w", padx=14, pady=(0, 8))
        return p

    # ------------------------------------------------------------ 传输面板
    def _build_bottom(self) -> ctk.CTkFrame:
        c = theme.colors()
        f = ctk.CTkFrame(self, corner_radius=0, fg_color=c["card2"], height=124)
        f.grid_columnconfigure(0, weight=1)
        f.grid_propagate(False)

        top = ctk.CTkFrame(f, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
        top.grid_columnconfigure(0, weight=1)
        self.l_stage = ctk.CTkLabel(top, text=t("就绪"), font=theme.font(12, "bold"), anchor="w")
        self.l_stage.grid(row=0, column=0, sticky="w")
        self.l_pct = ctk.CTkLabel(top, text="", font=theme.font(12, "bold"), anchor="e", text_color=c["accent"])
        self.l_pct.grid(row=0, column=1, sticky="e")
        self.pb = ctk.CTkProgressBar(f, height=12, corner_radius=6)
        self.pb.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 4))
        self.pb.set(0)

        info = ctk.CTkFrame(f, fg_color="transparent")
        info.grid(row=2, column=0, sticky="ew", padx=14)
        info.grid_columnconfigure(0, weight=1)
        self.l_file = ctk.CTkLabel(info, text="", font=theme.font(10), anchor="w", text_color=c["muted"])
        self.l_file.grid(row=0, column=0, sticky="w")
        self.b_cancel = ctk.CTkButton(info, text=t("取消"), width=70, height=26, font=theme.font(11),
                                      fg_color="transparent", border_width=1, border_color=c["danger"],
                                      text_color=c["danger"], hover_color="#7f1d1d", command=self.cancel_task,
                                      state="disabled")
        self.b_cancel.grid(row=0, column=1, rowspan=2, padx=(8, 0))
        self.l_speed = ctk.CTkLabel(info, text="", font=theme.font(10), anchor="w", text_color=c["muted"])
        self.l_speed.grid(row=1, column=0, sticky="w")

        # 快捷跳到日志页（日志已独立成选项卡，底部保持紧凑）
        ctk.CTkButton(info, text=t("📜 日志"), width=64, height=26, font=theme.font(10),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=lambda: self.show_page("log")).grid(
            row=0, column=2, rowspan=2, padx=(8, 0))
        return f

    # ------------------------------------------------------------ 日志页
    def _build_log_page(self, master) -> ctk.CTkFrame:
        c = theme.colors()
        self._log_buf = []
        self._log_rendered = 0
        self._log_visible = False

        p = self._panel(master)
        p.pack(fill="both", expand=True)

        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        ctk.CTkLabel(head, text=t("📜 传输日志"), font=theme.font(14, "bold")).pack(side="left", padx=4)
        for _t, _c in (("💾 保存日志", self.save_log), ("🗑 清空", self.clear_log)):
            ctk.CTkButton(head, text=_t, width=100, height=28, font=theme.font(11),
                          fg_color="transparent", border_width=1, border_color=c["line"],
                          text_color=c["text"], command=_c).pack(side="right", padx=(6, 0))
        self.l_logcount = ctk.CTkLabel(head, text=t("0 行"), font=theme.font(10), text_color=c["muted"])
        self.l_logcount.pack(side="right", padx=8)

        self.log_box = ctk.CTkTextbox(p, font=theme.font(10), fg_color=c["card"], wrap="none")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.log_box.configure(state="disabled")

        ctk.CTkLabel(p, text=t("日志同时保存在内存中，最多保留 2000 行；点「保存日志」可导出为 .log 文件"),
                     font=theme.font(10), text_color=c["muted"], anchor="w").grid(
            row=2, column=0, sticky="w", padx=14, pady=(0, 10))
        return p

    def save_log(self):
        from tkinter import filedialog
        p = filedialog.asksaveasfilename(
            title=t("保存日志"), defaultextension=".log",
            initialfile=f"MCSync-{time.strftime('%Y%m%d-%H%M%S')}.log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self._log_buf) + "\n")
            self.log(f"日志已保存：{p}")
            self.toast(t("日志已保存"))
        except OSError as e:
            self.toast(f"保存失败：{e}")

    def clear_log(self):
        self._log_buf = []
        self._log_rendered = 0
        try:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass
        self._upd_logcount()

    def _upd_logcount(self):
        try:
            self.l_logcount.configure(text=f"{len(self._log_buf)} 行")
        except Exception:
            pass

    def _flush_log(self):
        """每 250ms 批量渲染一次新增日志。旧实现每来一条就插入+滚动，
        传输时几百条消息会触发几百次重排，这正是卡顿的根因。"""
        try:
            if self._log_visible and self._log_rendered < len(self._log_buf):
                self.log_box.configure(state="normal")
                chunk = self._log_buf[self._log_rendered:]
                self.log_box.insert("end", "".join(s + "\n" for s in chunk))
                self._log_rendered = len(self._log_buf)
                lines = int(self.log_box.index("end-1c").split(".")[0])
                if lines > 600:
                    self.log_box.delete("1.0", f"{lines - 500}.0")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
                self._upd_logcount()
        except Exception:
            pass
        self.after(250, self._flush_log)

    # ================================================================== 启动
    def _boot(self):
        self.after(250, self._flush_log)
        self.log(t("MCSync 已启动"))
        # 启动分两步：先毫秒级读缓存让界面立刻可用，
        # 再后台重扫一次已知目录（只扫上次命中的根目录，不做全盘），
        # 这样新增/移除的实例每次开软件都会自动同步，却不用等全盘遍历。
        self.scan_instances(mode="auto", silent=True, then_rescan=True)
        self.test_connection(silent=True)
        self._setup_auto_sync()
        self._pump()

    # ---------------------------------------------------------- 定时同步
    def _setup_auto_sync(self):
        """定时同步：默认关闭，只有用户在设置里打开才会跑。"""
        self._auto_job = None
        self._schedule_auto_sync()

    def _schedule_auto_sync(self):
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None
        if not self.cfg.get("auto_sync_enabled", False):
            return
        try:
            mins = max(5, int(self.cfg.get("auto_sync_minutes", 30) or 30))
        except (TypeError, ValueError):
            mins = 30
        self._auto_job = self.after(mins * 60 * 1000, self._auto_sync_tick)
        self.log(f"定时同步已开启：每 {mins} 分钟自动上传一次当前存档")

    def _auto_sync_tick(self):
        self._auto_job = None
        try:
            if self.cfg.get("auto_sync_enabled", False) and not self.busy \
                    and self.cur_save is not None and self.cfg.get("server_url", ""):
                self.log(t("定时同步：开始自动上传"))
                self.do_upload()
        except Exception as e:
            self.log(f"定时同步失败：{e}")
        self._schedule_auto_sync()

    def refresh_auto_sync(self):
        """设置改了之后重新排程。"""
        self._schedule_auto_sync()

    def _pump(self):
        """把子线程投递的 UI 任务放到主线程执行"""
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def ui(self, fn: Callable):
        self._ui_queue.put(fn)

    # ================================================================== 服务器地址
    def _commit_url(self):
        """把地址写入配置；有变化时重建 Api 并重连。"""
        val = str(self.cfg.get("server_url", "") or "").strip()
        if not val:
            # 顶栏不再内联地址框，没有地址就提示去设置里填
            self.toast(t("请先在设置里填写服务器地址"))
            return
        if val != self.cfg.get("server_url", ""):
            self.cfg.set("server_url", val)
            self.api = Api(self.cfg.server_url)
            self._prepare_api()
            self.refresh_auto_sync()
            # 换了服务器，配对状态需要重新确认
            if self.cfg.get("server_token", ""):
                self.cfg.set("paired_server", "")
            self.log(f"服务器地址已保存：{self.cfg.server_url}")
        self.test_connection()

    # ================================================================== 实例
    #  扫描分三档，避免每次启动都全盘遍历：
    #    quick  —— 直接读取上次保存的实例路径，只校验目录是否还在（毫秒级）
    #    known  —— 只扫上次命中的根目录，能发现这些目录下新增的实例（较快）
    #    full   —— 全盘嗅探候选根目录（首次或手动点「全盘重扫」时才做）
    def scan_instances(self, mode: str = "auto", silent: bool = False,
                       then_rescan: bool = False, background: bool = False):
        """扫描实例。

        background=True 用于启动时的自动重扫：不占用忙碌态、不禁用按钮、
        结果没变化就不写日志。用户要的是"打开就有最新列表"，
        而不是"先被一个转圈状态挡住几秒"。
        """
        del silent  # 兼容旧调用签名，实际行为由 mode 决定
        if self.busy:
            return
        if mode == "auto":
            mode = "quick" if self.cfg.get("scanned_once", False) else "full"

        if mode == "quick":
            cached = self.cfg.get("known_instances", []) or []
            insts = []
            for d in cached:
                try:
                    inst = Instance.from_dict(d)
                except Exception:
                    continue
                if os.path.isdir(inst.game_dir):
                    insts.append(inst)
            if insts:
                self.log(f"已从缓存恢复 {len(insts)} 个实例（上次扫描结果）")
                self._apply_instances(insts, quick=True)
                if (then_rescan and self.cfg.get("rescan_on_start", True)
                        and not getattr(self, "_known_rescanned", False)):
                    # 界面已可用，再悄悄刷新一遍已知目录。
                    # 用 _known_rescanned 标记保证只跑一次：
                    # 之前没有这个守卫，一旦扫描结果为空就会反复退化重扫，
                    # 表现为"一直停在扫描中"。
                    self._known_rescanned = True
                    self.after(400, lambda: self.scan_instances(
                        mode="known", silent=True, background=True))
                return
            # 缓存为空或全部失效 -> 退化为全盘
            self.log(t("缓存实例已失效，转为全盘扫描…"))
            mode = "full"

        if background:
            # 后台静默：不进忙碌态、不禁用按钮、不打"开始"日志。
            # 之前每次启动都会显示"重扫已知目录…"并禁用上传/下载按钮，
            # 一旦磁盘慢就会被理解成"卡住了"。
            pass
        else:
            self.set_busy(True, "扫描游戏实例…" if mode == "full" else "重扫已知目录…")
            self.log("开始全盘扫描游戏实例…" if mode == "full" else "开始重扫已知实例目录…")

        def work():
            if mode == "full":
                scanner = InstanceScanner(roots=self.cfg.get("custom_roots", []),
                                          depth=int(self.cfg.get("scan_depth", 3)))
                return scanner.scan(sniff=True)
            # known：只检查已知实例的父目录一层，不做递归遍历。
            # 旧实现用 InstanceScanner(depth=2) 扫 known_roots，
            # 而 known_roots 会不断累加（历史上限 40 个）、还可能混进盘符根，
            # 于是"重扫"会演变成扫大半个盘 —— 表现为一直转圈不停。
            return InstanceScanner.fast_known_scan(
                self.cfg.get("known_instances", []) or [],
                [r for r in (self.cfg.get("custom_roots", []) or []) if os.path.isdir(r)],
            )

        def done(insts):
            # 关键修复：旧版本只有 full 模式才写回缓存（persist=(mode=="full")），
            # 于是点「实例重扫」扫到的新实例只存在于内存里，
            # 下次启动走 quick 又从旧缓存恢复 —— 表现就是"重扫完一刷新又变回原样"。
            # 现在 full 与 known 都落盘，且做合并而非覆盖。
            merge = (mode == "known")
            if merge and self._same_instances(insts):
                # 重扫结果与现有列表完全一致：不重建界面。
                # 重建会销毁卡片、重新选中实例（进而重新加载存档），
                # 那才是"启动后一直转圈"的根源。
                self.set_busy(False)
                if not background:
                    self.log("实例列表无变化")
                self.cfg.set("scanned_once", True)
                return
            self._apply_instances(insts, quick=False, persist=True, merge=merge)
            self.cfg.set("scanned_once", True)

        def fail(e):
            self.set_busy(False)
            # 后台重扫失败不必打断用户，记日志即可
            if not background:
                self.log(f"扫描失败：{e}")
            else:
                self.log(f"后台重扫未成功（不影响使用）：{e}")

        self.run_async(work, done, fail)

    def _same_instances(self, insts) -> bool:
        """判断重扫结果与当前列表是否一致（按实例根目录集合比较）。"""
        try:
            cur = {os.path.normcase(os.path.abspath(i.root))
                   for i in (self.instances or [])}
            new = {os.path.normcase(os.path.abspath(i.root)) for i in (insts or [])}
            return cur == new
        except Exception:
            return False

    def _apply_instances(self, insts, quick: bool = False, persist: bool = True,
                         merge: bool = False):
        """统一处理扫描结果：渲染、保存、恢复上次选中。

        merge=True 时与已缓存的实例合并（并集），用于「实例重扫」：
        只扫已知根目录时，放在别处的实例不该因为这次没扫到就被抹掉。
        """
        if merge:
            insts = self._merge_instances(insts)

        self.instances = insts
        self.render_instances()
        self.set_busy(False)
        self.log(f"识别到 {len(insts)} 个游戏实例")

        if persist and insts:
            # 保存实例路径与其所在根目录，下次启动直接复用
            self.cfg.set("known_instances", [i.to_dict() for i in insts])
            roots = [r for r in (self.cfg.get("known_roots", []) or []) if os.path.isdir(r)]
            for i in insts:
                parent = os.path.dirname(os.path.abspath(i.root))
                # 盘符根（D:\ 之类）不记录：扫它等于全盘遍历，代价极高
                if os.path.dirname(parent) == parent:
                    continue
                if os.path.isdir(parent) and parent not in roots:
                    roots.append(parent)
            # 上限压到 20 并剔除失效项，防止目录越攒越多、重扫越来越慢
            roots = [r for r in roots if os.path.isdir(r)][:20]
            self.cfg.set("known_roots", roots)

        if not insts:
            self.toast(t("未自动识别到实例，可点 ➕ 手动添加目录"))
            # 0 结果时给出排查线索，避免"扫了但不知道扫了哪里"
            try:
                from core.scanner import InstanceScanner
                roots = InstanceScanner(roots=self.cfg.get("custom_roots", []),
                                        depth=int(self.cfg.get("scan_depth", 3))).candidate_roots()
                self.log(t("已扫描的候选根目录："))
                for r in roots[:12]:
                    self.log(f"  - {r}")
                self.log(t("若启动器是绿色版且不在上述目录，请在设置里手动添加其所在文件夹"))
            except Exception:
                self.log(t("提示：绿色版启动器请在「设置 → 额外实例目录」手动添加"))


    def _merge_instances(self, insts):
        """把本次扫描结果与已缓存实例合并成并集（按实例根目录去重）。

        同时剔除已经不存在的旧条目，保证缓存不会越攒越多。
        """
        merged = {}
        for i in insts:
            merged[os.path.abspath(i.root)] = i
        for d in (self.cfg.get("known_instances", []) or []):
            try:
                old_inst = Instance.from_dict(d)
            except Exception:
                continue
            key = os.path.abspath(old_inst.root)
            if key in merged:
                continue
            # 目录还在就保留（可能只是这次没扫到它所在的根目录）
            if os.path.isdir(old_inst.game_dir) or os.path.isdir(old_inst.root):
                merged[key] = old_inst
        out = list(merged.values())
        out.sort(key=lambda x: (x.name or "").lower())
        added = len(out) - len(insts)
        if added > 0:
            self.log(f"已合并缓存中的 {added} 个实例")
        return out

    def render_instances(self):
        for w in self.inst_box.winfo_children():
            w.destroy()
        for i, inst in enumerate(self.instances):
            card = InstanceCard(self.inst_box, inst, on_click=lambda x=inst: self.select_instance(x))
            card.grid(row=i, column=0, sticky="ew", padx=6, pady=3)
            if self.cur_instance and inst.key == self.cur_instance.key:
                card.set_selected(True)
            inst._card = card  # type: ignore[attr-defined]
        self.l_inst_count.configure(text=f"共 {len(self.instances)} 个实例 · 点击 ➕ 可添加自定义目录")

    def select_instance(self, inst: Instance):
        self.cur_instance = inst
        self.cur_save = None
        self.cfg.set("last_instance", inst.root)
        for w in self.inst_box.winfo_children():
            if isinstance(w, InstanceCard):
                w.set_selected(w.inst.key == inst.key)
        self.l_save_title.configure(text=f"存档 · {inst.name}")
        self.load_saves(inst)

    # ---- 两个重扫入口（对应实例面板头部的按钮）
    def rescan_full(self):
        """全盘重扫：重新嗅探所有候选根目录，能发现全新位置的实例。"""
        self.cfg.set("scanned_once", True)
        self.scan_instances(mode="full")

    def rescan_known(self):
        """实例重扫：只扫上次命中的根目录，快速发现新增/移除的实例。"""
        self.scan_instances(mode="known")

    def add_instance_dir(self):
        d = filedialog.askdirectory(title=t("选择启动器目录或游戏目录"))
        if not d:
            return
        roots = list(self.cfg.get("custom_roots", []) or [])
        if d not in roots:
            roots.append(d)
            self.cfg.set("custom_roots", roots)
        self.toast(t("已添加目录，正在重新扫描…"))
        self.scan_instances(mode="known")

    # ================================================================== 存档
    def load_saves(self, inst: Optional[Instance], quick: bool = True):
        """读取实例的存档列表。

        quick=True 时只解析 level.dat（毫秒级），不统计目录大小。
        为什么要分两档：
          统计大小要遍历存档下每一个文件，一个几百 MB 的存档就是上万次 stat，
          在机械盘上要好几秒。启动时如果对每个实例都做一次，
          20 个实例就是几十秒 —— 表现正是"扫了半天停不下来"。
        所以先秒出列表让用户能操作，大小再后台补（见 _fill_sizes）。
        """
        for w in self.save_box.winfo_children():
            w.destroy()
        if inst is None:
            self.l_sel.configure(text=t("请先选择左侧的游戏实例"))
            return
        self.l_sel.configure(text=t("正在读取存档…"))
        self.saves = []

        def work():
            return InstanceScanner.list_saves(inst, quick=quick)

        def done(saves: List[SaveInfo]):
            self.saves = saves
            self.render_saves()
            if saves:
                self.select_save(saves[0])
            else:
                self.l_sel.configure(text=f"该实例下没有找到存档（{inst.saves_dir}）")
                self._sync_prune_if_visible()
            self.log(f"实例「{inst.name}」下发现 {len(saves)} 个存档")
            if quick and saves:
                self._fill_sizes(saves)

        self.run_async(work, done)

    def _fill_sizes(self, saves: List[SaveInfo]):
        """后台补全存档体积，完成后只刷新数字，不重建列表。

        与 load_saves 分开是为了让界面先可用：用户不必等统计完才能点。
        """
        def work():
            from concurrent.futures import ThreadPoolExecutor
            def one(si: SaveInfo):
                try:
                    size, count = InstanceScanner.stat_save(si.path)
                    si.size, si.file_count = size, count
                except Exception:
                    si.size, si.file_count = 0, 0
            with ThreadPoolExecutor(max_workers=min(4, len(saves))) as ex:
                list(ex.map(one, saves))
            return True

        def done(_):
            # 只更新卡片上的数字，避免整列表重建导致选中态丢失
            self.refresh_save_sizes()

        self.run_async(work, done)

    def refresh_save_sizes(self):
        """就地刷新存档卡片上的体积/文件数。"""
        try:
            for w in self.save_box.winfo_children():
                if hasattr(w, "refresh_size"):
                    w.refresh_size()
        except Exception:
            pass

    def render_saves(self):
        for w in self.save_box.winfo_children():
            w.destroy()
        if not self.saves:
            ctk.CTkLabel(self.save_box, text=t("暂无存档"), font=theme.font(12),
                         text_color=theme.colors()["muted"]).grid(row=0, column=0, pady=20)
            return
        for i, s in enumerate(self.saves):
            card = SaveCard(self.save_box, s, on_click=lambda x=s: self.select_save(x),
                            on_double=lambda x=s: self.do_upload())
            card.grid(row=i, column=0, sticky="ew", padx=6, pady=4)
            s._card = card  # type: ignore[attr-defined]
            # 重建/刷新后保持选中高亮（主题切换会整体重建）
            if self.cur_save and getattr(self.cur_save, "path", "") == s.path:
                card.set_selected(True)

    def select_save(self, save: SaveInfo):
        self.cur_save = save
        self.cfg.set("last_save", save.path)
        for w in self.save_box.winfo_children():
            if isinstance(w, SaveCard):
                w.set_selected(w.save.path == save.path)
        st = "已同步" if save.synced else "未同步"
        self.l_sel.configure(
            text=f"已选择：{save.display_name}　|　{save.size_text}　|　云端名：{save.cloud_name}　|　{st}")
        self._sync_prune_if_visible()

    def _sync_prune_if_visible(self):
        """存档/实例变化时，若正停留在区块清理页就同步下拉与能力检测。"""
        if getattr(self, "cur_page", "") != "prune" or not hasattr(self, "prune_page"):
            return
        try:
            self.prune_page.sync_selection(self.instances, self.saves,
                                           self.cur_instance, self.cur_save)
        except Exception:
            pass

    def open_save_dir(self):
        p = self.cur_save.path if self.cur_save else (self.cur_instance.saves_dir if self.cur_instance else "")
        if p and os.path.isdir(p):
            self._open_path(os.path.dirname(p) if self.cur_save else p)

    def _open_path(self, p: str):
        try:
            if os.name == "nt":
                os.startfile(p)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", p])
        except Exception:
            pass

    # ================================================================== 云端
    def test_connection(self, silent: bool = False):
        url = str(self.cfg.get("server_url", "") or "").strip()
        if not url:
            if not silent:
                self.toast(t("请先在设置里填写服务器地址"))
            return
        self.cfg.set("server_url", url)
        self.api = Api(self.cfg.server_url)
        self._prepare_api()
        self.l_status.configure(text=t("● 连接中…"), text_color=theme.colors()["muted"])

        def work():
            return self.api.test()

        def done(r):
            c = theme.colors()
            self.l_status.configure(text=f"● 已连接 · {r.get('ping_ms', 0)}ms", text_color=c["ok"])
            self.l_cloud.configure(text="服务端在线 · 存档 " + str(self._safe_stat()))
            self.refresh_cloud()
            if not silent:
                self.toast(f"连接成功（{r.get('ping_ms', 0)} ms）")
            self.log(f"已连接服务器 {self.cfg.server_url}")

        def fail(e):
            c = theme.colors()
            self.l_status.configure(text=t("● 连接失败"), text_color=c["danger"])
            self.l_cloud.configure(text=str(e))
            if not silent:
                self.toast(str(e))
            self.log(f"连接失败：{e}")

        self.run_async(work, done, fail)

    def _safe_stat(self) -> str:
        try:
            return str(self.api.stats().get("save_count", 0))
        except Exception:
            return "—"

    def refresh_cloud(self):
        def work():
            return self.api.list_saves()

        def done(saves):
            self.cloud = saves
            self.render_cloud()
            try:
                st = self.api.stats()
                self.l_cloud.configure(
                    text=f"云端 {st.get('save_count', 0)} 个存档 · {st.get('version_count', 0)} 个版本 · "
                         f"占用 {human_size(st.get('disk_usage', 0))}")
            except Exception:
                self.l_cloud.configure(text=f"云端 {len(saves)} 个存档")
            # 自动选中与当前本地存档同名的云端存档
            if self.cur_save:
                for d in saves:
                    if d["name"] == self.cur_save.cloud_name:
                        self.cur_cloud = d
                        break

        def fail(e):
            self.l_cloud.configure(text=str(e))

        self.run_async(work, done, fail)

    def render_cloud(self):
        """云端存档按整合包分组显示。

        分组键用「整合包名」，没有整合包信息的老存档归入「未分类」。
        这样「1.20.1-Forge 下的存档」和「1.12.2-Forge 下的存档」即使同名也不会混在一起。
        """
        for w in self.cloud_box.winfo_children():
            w.destroy()
        if not self.cloud:
            ctk.CTkLabel(self.cloud_box, text=t("云端暂无存档"), font=theme.font(12),
                         text_color=theme.colors()["muted"]).grid(row=0, column=0, pady=20)
            return

        groups: Dict[str, list] = {}
        order: List[str] = []
        for d in self.cloud:
            key = (d.get("instance", "") or "").strip() or "未分类"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(d)

        # 已归类的排前面，「未分类」沉底
        order.sort(key=lambda k: (k == "未分类", k.lower()))

        row = 0
        c = theme.colors()
        for key in order:
            head = ctk.CTkFrame(self.cloud_box, fg_color="transparent")
            head.grid(row=row, column=0, sticky="ew", padx=6, pady=(10 if row else 2, 2))
            icon = "📦" if key != "未分类" else "🗂"
            ctk.CTkLabel(head, text=f"{icon} {key}", font=theme.font(11, "bold"),
                         text_color=c["accent"] if key != "未分类" else c["muted"],
                         anchor="w").pack(side="left", padx=(6, 4))
            ctk.CTkLabel(head, text=f"{len(groups[key])} 个", font=theme.font(9),
                         text_color=c["muted"]).pack(side="left")
            row += 1

            for d in groups[key]:
                card = CloudCard(self.cloud_box, d,
                                 on_click=lambda x=d: self.select_cloud(x),
                                 on_manage=lambda x=d: (self.select_cloud(x), self.manage_versions()))
                card.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
                if self.cur_cloud and d.get("name") == self.cur_cloud.get("name"):
                    card.set_selected(True)
                row += 1

    def select_cloud(self, d: dict):
        self.cur_cloud = d
        self.cfg.set("last_cloud_save", d.get("name", ""))
        # 高亮选中的云端卡片，视觉反馈与本地存档一致（不再只用文字提示）
        for w in self.cloud_box.winfo_children():
            if isinstance(w, CloudCard):
                w.set_selected(w.data.get("name") == d.get("name"))
        self.l_cloud.configure(text=f"已选中：{d.get('name', '')}（点『下载到本地』保存到当前实例）")

    def open_web(self):
        url = self.cfg.server_url
        if url:
            webbrowser.open(url)
        else:
            self.toast(t("请先填写服务器地址"))

    def assign_instance(self):
        """手动指定云端存档所属整合包（老存档归入「未分类」后用它归类）。"""
        d = self.cur_cloud
        if not d:
            self.toast(t("请先选中一个云端存档"))
            return
        name = d.get("name", "")
        cur = (d.get("instance", "") or "").strip()

        # 候选：本地已识别的实例名 + 当前实例 + 清空选项
        names = []
        for i in getattr(self, "instances", []) or []:
            if i.name and i.name not in names:
                names.append(i.name)
        if cur and cur not in names:
            names.insert(0, cur)

        dlg = _InstancePickDialog(self, name, cur, names)
        picked = dlg.result
        if picked is None:
            return
        if picked == cur:
            return

        def work():
            self.api.set_instance(name, picked)
            return picked

        def done(v):
            self.toast(f"已归类到「{v or '未分类'}」")
            self.log(f"云端存档「{name}」已归类为：{v or '未分类'}")
            self.refresh_cloud()

        self.run_async(work, done, lambda e: self.toast(f"归类失败：{e}"))

    def manage_versions(self):
        name = (self.cur_cloud or {}).get("name") or (self.cur_save.cloud_name if self.cur_save else "")
        if not name:
            self.toast(t("请先选择云端存档"))
            return

        def work():
            return self.api.versions(name)

        def done(vers):
            VersionDialog(self, name, vers,
                          on_action=lambda a, v, vt: self._version_action(a, name, v, vt))

        self.run_async(work, done, lambda e: self.toast(str(e)))

    def _version_action(self, action: str, name: str, vid: int, vtime: str = ""):
        label = vtime or t("该次上传")
        if action == "delete":
            dlg = ConfirmDialog(self, "删除版本",
                                f"确定删除「{name}」在 {label} 的这次上传？此操作不可撤销。",
                                ok_text="删除", danger=True)
            if not dlg.result:
                return
            self.run_async(lambda: self.api.delete_version(name, vid),
                           lambda r: (self.toast(t("已删除")), self.refresh_cloud()))
            return
        if action == "restore":
            self.run_async(lambda: self.api.restore(name, vid),
                           lambda r: (self.toast(f"已回滚到 {label} 的上传（新快照已创建）"),
                                      self.refresh_cloud()))
            return
        if action == "download":
            self.do_download(vid=vid, cloud_name=name)

    def delete_cloud(self):
        name = (self.cur_cloud or {}).get("name")
        if not name:
            self.toast(t("请先选择云端存档"))
            return
        dlg = ConfirmDialog(self, "删除云端存档",
                            f"确定删除云端存档「{name}」的全部版本？",
                            ok_text="删除", danger=True, extra="本地存档不受影响。")
        if not dlg.result:
            return
        self.run_async(lambda: self.api.delete_save(name),
                       lambda r: (self.toast(t("已删除")), self.refresh_cloud()))

    # ================================================================== 同步
    def do_upload(self):
        if self.busy:
            return
        save = self.cur_save
        if save is None:
            self.toast(t("请先在中间选择一个本地存档"))
            return
        if not self.cfg.server_url:
            self.toast(t("请先填写并测试服务器地址"))
            return
        self._cancel = CancelToken()
        engine = SyncEngine(self.api, self._progress, self._cancel, log=self.log)
        self.set_busy(True, "上传中")

        # 上传时把所属整合包（实例名）一起提交，云端才能按整合包归类。
        # 不同整合包里同名存档因此不会混在一起。
        inst_name = (self.cur_instance.name if self.cur_instance else "") or ""
        if not inst_name and getattr(save, "instance", None):
            inst_name = save.instance.name or ""

        def work():
            return engine.upload(
                save.path, save.cloud_name,
                note=f"来自 {os.path.basename(save.path)}",
                resolver=lambda conflicts: self.ask_conflicts(conflicts, "upload"),
                full_hash=bool(self.cfg.get("force_full_hash", False)),
                instance=inst_name,
            )

        def done(r):
            self.set_busy(False)
            vt = r.get("version_time") or t("刚刚")
            self.toast(f"上传完成（{vt}）· 实际传输 {human_size(r['uploaded_bytes'])}")
            self.load_saves(self.cur_instance)
            self.refresh_cloud()
            self.progress_reset(f"上传完成 · {vt} · {r['file_count']} 文件 · "
                                f"复用块 {r['reused_chunks']}")

        def fail(e):
            self.set_busy(False)
            self.toast(str(e))
            self.progress_reset("上传失败")

        self.run_async(work, done, fail)

    def _cloud_record(self, name: str) -> dict:
        """取云端存档记录（含所属整合包名），列表里没有就返回空字典。"""
        for d in getattr(self, "cloud", []) or []:
            if d.get("name") == name:
                return d
        return {}

    @staticmethod
    def _norm_inst(s: str) -> str:
        """实例名归一化：忽略大小写、空格与分隔符差异，便于跨机器匹配。

        多人使用时，同一个整合包在不同电脑上可能有细微命名差异
        （大小写、空格、中英文括号），归一化后能稳定匹配上。
        """
        t = (s or "").strip().lower()
        for ch in (" ", "_", "-", "（", "）", "(", ")", "【", "】", "[", "]", ".", "+"):
            t = t.replace(ch, "")
        return t

    def _find_instance_by_name(self, inst_name: str):
        """按整合包名在本机实例里找同名实例，找不到返回 None。"""
        if not inst_name:
            return None
        want = self._norm_inst(inst_name)
        if not want:
            return None
        for i in getattr(self, "instances", []) or []:
            if self._norm_inst(getattr(i, "name", "")) == want:
                return i
        return None

    def _resolve_download_target(self, name: str) -> Optional[str]:
        """决定云端存档下载到本机的哪个目录。

        多人协作的关键：云端记录里带着「所属整合包名」和「原始存档名」，
        所以别人上传的存档，下载时会自动落到本机同名整合包的 saves 目录下，
        并被还原成原来的存档名（而不是带限定后缀的云端键名）。
        """
        rec = self._cloud_record(name)
        inst_name = (rec.get("instance", "") or "").strip()

        # 整合包配置：装到同名实例的 config 目录，而不是塞进 saves
        if rec.get("kind") == "config":
            tgt_inst = self._find_instance_by_name(inst_name) or self.cur_instance
            if tgt_inst is None:
                self.toast(t("本机没有同名的整合包实例，无法装载配置"))
                return None
            d = tgt_inst.config_dir
            self.log(f"整合包配置将装载到 {d}")
            return d

        # 云端键可能是「MyWorld (1.12.2-Forge)」，真实存档名用 display
        save_name = (rec.get("display") or "").strip() or name

        match = self._find_instance_by_name(inst_name)
        default_dir = ""
        if match is not None:
            default_dir = os.path.join(match.saves_dir, save_name)
        elif self.cur_instance is not None:
            # 没有同名实例时给个兜底：当前实例的存档目录
            default_dir = os.path.join(self.cur_instance.saves_dir, save_name)

        names: List[str] = []
        dirs: dict = {}
        for i in getattr(self, "instances", []) or []:
            nm = getattr(i, "name", "") or ""
            if nm and nm not in names:
                names.append(nm)
                dirs[nm] = getattr(i, "saves_dir", "") or ""

        # 命中同名实例且本地还没有这个存档 -> 直接装过去，不再打扰用户
        if match is not None and not os.path.isdir(default_dir):
            self.log(f"云端存档「{save_name}」来自整合包「{inst_name}」，"
                     f"已匹配本机同名实例，装入 {default_dir}")
            return default_dir

        dlg = _DownloadTargetDialog(self, save_name, inst_name, names, dirs, default_dir)
        return dlg.result

    def _instance_of_dir(self, path: str):
        """判断某个存档目录属于哪个本机实例，找不到返回 None。"""
        try:
            parent = os.path.dirname(os.path.abspath(path))
        except Exception:
            return None
        for i in getattr(self, "instances", []) or []:
            sd = getattr(i, "saves_dir", "") or ""
            try:
                if sd and os.path.abspath(sd) == parent:
                    return i
            except Exception:
                continue
        return None

    def do_upload_config(self):
        """上传当前实例的 config 目录。

        多人玩同一个整合包时，服务端/模组配置必须一致，否则各人世界表现会不一样。
        配置作为独立条目上云（kind=config），不会和存档混在一起。
        """
        if self.busy:
            return
        inst = self.cur_instance
        if inst is None:
            self.toast(t("请先在左侧选择一个游戏实例"))
            return
        cdir = inst.config_dir
        if not os.path.isdir(cdir):
            self.toast(f"该实例没有 config 目录：{cdir}")
            return
        if not self.cfg.get("server_url", ""):
            self.toast(t("请先填写并测试服务器地址"))
            return
        self._cancel = CancelToken()
        engine = SyncEngine(self.api, self._progress, self._cancel, log=self.log)
        self.set_busy(True, "上传整合包配置")

        def work():
            return engine.upload(cdir, "整合包配置", instance=inst.name,
                                 note="整合包配置", kind="config")

        def done(r):
            self.set_busy(False)
            vt = r.get("version_time") or t("刚刚")
            self.toast(f"整合包配置已上传（{vt}）· {r['file_count']} 个文件")
            self.refresh_cloud()
            self.progress_reset(f"配置上传完成 · {vt} · {r['file_count']} 文件")

        def fail(e):
            self.set_busy(False)
            self.log(f"配置上传失败：{e}")
            self.toast(f"上传失败：{e}")
            self.progress_reset("配置上传失败")

        self.run_async(work, done, fail)

    def do_download(self, vid: Optional[int] = None, cloud_name: Optional[str] = None):
        if self.busy:
            return
        name = cloud_name or (self.cur_cloud or {}).get("name")
        if not name and self.cur_save:
            name = self.cur_save.cloud_name
        if not name:
            self.toast(t("请先选择右侧的云端存档"))
            return
        target = self._resolve_download_target(name)
        if not target:
            return
        self._cancel = CancelToken()
        engine = SyncEngine(self.api, self._progress, self._cancel, log=self.log)
        self.set_busy(True, "下载中")
        tgt = target

        def work():
            return engine.download(
                tgt, name, vid=vid,
                resolver=lambda conflicts: self.ask_conflicts(conflicts, "download"),
            )

        def done(r):
            self.set_busy(False)
            vt = r.get("version_time") or t("刚刚")
            pinfo = r.get("playerdata") or {}
            if pinfo.get("status") == "fixed":
                self.toast(f"下载完成（{vt}）· 已解除共享玩家数据，进世界会是你自己的角色")
            elif pinfo.get("status") == "running":
                self.toast(f"下载完成（{vt}）· 游戏开着，共享玩家数据未处理")
            else:
                self.toast(f"下载完成（{vt}）· 写入 {r['downloaded_files']} 个文件")
            # 存档装到了哪个实例，就把界面切到那个实例，
            # 否则用户会以为没下载成功（尤其自动匹配到同名实例时）
            host = self._instance_of_dir(tgt)
            if host is not None and host is not self.cur_instance:
                self.select_instance(host)
            else:
                self.load_saves(self.cur_instance)
            self.progress_reset(f"下载完成 · {vt} · 写入 {human_size(r['written_bytes'])} · "
                                f"跳过未变 {r['unchanged']}")

        def fail(e):
            self.set_busy(False)
            self.toast(str(e))
            self.progress_reset("下载失败")

        self.run_async(work, done, fail)

    # ---------------------------------------------------------- 冲突弹窗（主线程）
    def ask_conflicts(self, conflicts: List[dict], direction: str) -> Dict[str, str]:
        holder: Dict[str, Dict[str, str]] = {}
        ev = threading.Event()

        def show():
            try:
                dlg = ConflictDialog(self, conflicts, direction)
                self.wait_window(dlg)
                holder["r"] = dlg.result or {}
            except Exception:
                holder["r"] = {}
            finally:
                ev.set()

        self.ui(show)
        ev.wait(timeout=600)
        res = holder.get("r") or {}
        for c in conflicts:
            res.setdefault(c["path"], "local")
        return res

    def ask_confirm(self, title: str, msg: str, ok_text: str = "确定", danger: bool = False,
                    extra: Optional[str] = None) -> bool:
        holder: Dict[str, bool] = {}
        ev = threading.Event()

        def show():
            try:
                dlg = ConfirmDialog(self, title, msg, ok_text=ok_text, danger=danger, extra=extra)
                holder["r"] = bool(dlg.result)
            except Exception:
                holder["r"] = False
            finally:
                ev.set()

        self.ui(show)
        ev.wait(timeout=300)
        return bool(holder.get("r"))

    # ================================================================== 基础设施
    def run_async(self, work: Callable, done: Callable, fail: Optional[Callable] = None):
        def runner():
            try:
                r = work()
                self.ui(lambda: done(r))
            except MCSyncError as e:
                self.log(f"错误：{e}")
                self.ui(lambda err=e: fail(err) if fail else self.toast(str(err)))
            except Exception as e:  # noqa: BLE001
                self.log(traceback.format_exc())
                self.ui(lambda err=e: fail(err) if fail else self.toast(f"发生错误：{err}"))

        self._executor.submit(runner)

    def set_busy(self, on: bool, stage: str = ""):
        self.busy = on
        st = "disabled" if on else "normal"
        for b in (self.b_upload, self.b_download, self.b_scan_saves):
            try:
                b.configure(state=st)
            except Exception:
                pass
        self.b_cancel.configure(state="normal" if on else "disabled")
        if on:
            self.l_stage.configure(text=stage or "处理中…")
            self.pb.configure(mode="indeterminate")
            self.pb.start()

    def _on_progress(self, st: dict):
        def upd():
            c = theme.colors()
            stage = str(st.get("stage", ""))
            if stage:
                self.l_stage.configure(text=stage)
            total = int(st.get("total_bytes", 0) or 0)
            done = int(st.get("done_bytes", 0) or 0)
            ti = int(st.get("total_items", 0) or 0)
            di = int(st.get("done_items", 0) or 0)
            if total > 0:
                self.pb.configure(mode="determinate")
                self.pb.stop()
                self.pb.set(min(1.0, done / total))
                self.l_pct.configure(text=f"{done / total * 100:.1f}%")
                self.l_speed.configure(
                    text=f"{human_size(done)} / {human_size(total)}　·　{human_speed(float(st.get('speed', 0)))}"
                         f"　·　剩余 {human_eta(float(st.get('eta', 0)) if done < total else 0)}"
                         f"　·　{di}/{ti} 项")
            elif ti > 0:
                self.pb.configure(mode="determinate")
                self.pb.stop()
                self.pb.set(min(1.0, di / max(1, ti)))
                self.l_pct.configure(text=f"{di}/{ti}")
                self.l_speed.configure(text=f"{di} / {ti} 项")
            f = str(st.get("file", "") or "")
            self.l_file.configure(text=("当前：" + f) if f else "")
            if st.get("finished"):
                if st.get("error"):
                    self.l_stage.configure(text=f"失败：{st['error']}", text_color=c["danger"])
                self.pb.stop()

        self.ui(upd)

    def progress_reset(self, text: str = "就绪"):
        def upd():
            self.l_stage.configure(text=text, text_color=theme.colors()["text"])
            self.l_pct.configure(text="")
            self.l_file.configure(text="")
            self.l_speed.configure(text="")
            self.pb.stop()
            self.pb.configure(mode="determinate")
            self.pb.set(0)

        self.ui(upd)

    def cancel_task(self):
        self._cancel.cancel()
        self.log(t("已请求取消当前任务…"))
        self.toast(t("正在取消…"))

    def log(self, msg: str):
        """只写入内存缓冲，渲染交给 _flush_log 定时批量完成。"""
        self._log_buf.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self._log_buf) > 2000:
            drop = len(self._log_buf) - 2000
            del self._log_buf[:drop]
            self._log_rendered = max(0, self._log_rendered - drop)

    def _prepare_api(self):
        """配置了账号时，确保本机已在服务器完成登录并取回最新角色。"""
        try:
            # 已配对时 ensure_login 不再重复登记私钥，但会 refresh_me()，
            # 保证 self.me 始终有服务端返回的真实角色（管理员改权限即时生效）。
            did_login = self.api.ensure_login()
            who = self.api.me or {}
            if who.get("username"):
                role = t("管理员") if who.get("role") == "admin" else t("普通用户")
                prefix = "已登录" if did_login else "身份已确认"
                self.log(f"{prefix}：{who.get('display') or self.cfg.get('username','')}（{role}）")
        except AuthError as e:
            self.log(f"登录失败：{e}")
        # 顶栏登录区跟着状态走：登录成功要立刻从"表单"切成"已登录身份"
        try:
            self._refresh_login_row()
        except Exception:
            pass

    def toast(self, msg: str):
        def upd():
            c = theme.colors()
            _toast = ctk.CTkLabel(self, text=msg, font=theme.font(12), fg_color=c["card"],
                             corner_radius=10, border_width=1, border_color=c["accent"],
                             padx=16, pady=8, text_color=c["text"])
            _toast.place(relx=0.5, rely=0.92, anchor="center")
            self.after(2600, _toast.destroy)

        self.ui(upd)

    # ---------------------------------------------------------- 主题
    def toggle_theme(self):
        cur = str(ctk.get_appearance_mode()).lower()
        new = "light" if cur == "dark" else "dark"
        ctk.set_appearance_mode(new)
        self.cfg.set("theme_mode", "dark" if new == "dark" else "light")
        # 整体重建界面，而不是逐个改色（见 rebuild_ui 说明）
        self.rebuild_ui()

    def rebuild_ui(self):
        """销毁并重建整个界面。

        为什么不用"遍历控件改色"：本程序大量控件显式传了 fg_color/text_color，
        这些控件不会跟随 set_appearance_mode 变化；只改一部分就会出现
        "一半深一半浅"。重建能保证所有控件都用新主题色创建，不会有遗漏。
        """
        snap = dict(
            instances=list(getattr(self, "instances", []) or []),
            saves=list(getattr(self, "saves", []) or []),
            cloud=list(getattr(self, "cloud", []) or []),
            cur_instance=getattr(self, "cur_instance", None),
            cur_save=getattr(self, "cur_save", None),
            cur_cloud=getattr(self, "cur_cloud", None),
            log_buf=list(getattr(self, "_log_buf", []) or []) if isinstance(
                getattr(self, "_log_buf", []), list) else [],
            page=getattr(self, "cur_page", "local"),
        )
        for w in list(self.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._build_ui()

        # 还原数据与被选中项
        self.instances = snap["instances"]
        self.saves = snap["saves"]
        self.cloud = snap["cloud"]
        self.cur_instance = snap["cur_instance"]
        self.cur_save = snap["cur_save"]
        self.cur_cloud = snap["cur_cloud"]
        self._log_buf = snap["log_buf"]
        self._log_rendered = 0
        try:
            self.render_instances()
            self.render_saves()
            self.render_cloud()
            self._upd_logcount()
            self.show_page(snap["page"])
            self.progress_reset()
        except Exception:
            pass

    def refresh_theme(self):
        c = theme.colors()
        self.configure(fg_color=c["card"])
        for panel in (self.left, self.center, self.right):
            panel.configure(fg_color=c["card2"], border_color=c["line"])
        # 侧栏与当前选中页也要跟着换色，否则会出现"一半深一半浅"
        try:
            self.sidebar.configure(fg_color=c["card2"], border_color=c["line"])
            for k, b in self.tab_btns.items():
                if k == self.cur_page:
                    b.configure(fg_color=c["accent"], text_color="#ffffff", hover_color=c["accent_hover"])
                else:
                    b.configure(fg_color="transparent", text_color=c["text"], hover_color=c["card"])
        except Exception:
            pass
        self.render_instances()
        self.render_saves()
        self.render_cloud()
        if hasattr(self, "prune_page"):
            try:
                self.prune_page.refresh_theme()
            except Exception:
                pass
        if hasattr(self, "playerdata_page"):
            try:
                self.playerdata_page.refresh_theme()
            except Exception:
                pass

    def open_settings(self):
        def saved():
            self.toast(t("设置已保存"))
            self.api = Api(self.cfg.server_url)
            self._prepare_api()
            mode = self.cfg.get("theme_mode", "dark")
            ctk.set_appearance_mode(mode)
            if not self.cfg.get("auto_scale", True):
                theme.apply(mode, float(self.cfg.get("ui_scale", 1.0)))
            self.refresh_theme()
            self.scan_instances(silent=True)

        SettingsDialog(self, on_saved=saved)

    # ---------------------------------------------------------- 关闭
    def on_close(self):
        try:
            # 退出前保存窗口位置与尺寸，下次打开恢复
            self.cfg.set("window_geometry", self.geometry())
            self.cfg.save()
        except Exception:
            pass
        self._cancel.cancel()
        self.destroy()


def main():
    """启动流程。

    顺序：先建主窗口（隐藏）→ 再弹登录 → 登录结果回灌主窗口。
    主窗口先建好是为了登录成功后立刻可用，不用再等一次构建；
    全程隐藏避免"小窗口闪一下再撑大"。
    """
    app = App()
    app.withdraw()          # 登录期间保持隐藏，登录完再显示

    def after_login(res):
        app.on_login_result(res)
        app.deiconify()
        app.lift()
        app.focus_force()

    try:
        from ui.login_window import LoginWindow
        LoginWindow(on_success=after_login, auto=True)
    except Exception as e:
        # 登录窗口起不来不能挡住本地功能
        try:
            app.log(f"登录窗口初始化失败：{e}")
        except Exception:
            pass
        app.deiconify()

    app.mainloop()


if __name__ == "__main__":
    main()
