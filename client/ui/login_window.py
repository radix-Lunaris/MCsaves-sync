# -*- coding: utf-8 -*-
"""
登录窗口 —— 打开软件的第一屏。

为什么独立成窗口而不是塞在设置里：
  登录是使用本软件的第一步。藏在设置里等于让用户先猜
  "我是不是得去哪里连一下"，第一次打开就会卡住。
  现在打开即见，填一次就能记住，之后自动登录直接进主界面。

凭据保存：
  口令用系统加密区存放（Windows 走 DPAPI，与当前登录用户绑定），
  不写明文；"记住我"关掉则不保存，下次仍需输入。
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from typing import Optional

import customtkinter as ctk

from core.config import cfg
from core.i18n import t
from ui import theme
from ui import design as ds



class LoginWindow(ctk.CTkToplevel):
    """登录窗口。

    用法：
        LoginWindow(on_success=app.on_logged_in)
    若本地已有可用凭据，会先尝试自动登录（后台线程），
    成功则直接回调，不再显示表单。
    """

    W, H = 460, 560

    def __init__(self, on_success, auto: bool = True):
        # 先建主窗口再隐藏，避免 Tk 默认小窗口闪一下
        super().__init__()
        self.withdraw()
        self.on_success = on_success
        self.result: Optional[dict] = None
        self._busy = False
        self._auto_tried = False
        # 自动登录是后台线程，用户可能不等它、直接改地址。
        # 每点一次「停止自动登录」就递增代次，旧线程的结果一律丢弃，
        # 避免它晚到的"登录失败"提示盖掉用户刚填好的新地址状态。
        self._auto_gen = 0

        self.title(t("登录 MCSync"))
        self.resizable(False, False)
        self.configure(fg_color=theme.colors().get("bg"))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_icon()

        self._build()
        self._fit()
        self.deiconify()
        # 与对话框同样的教训：grab_set 必须能失败降级，不能在窗口不可见时
        # 抛异常把整个启动流程打断、又把主窗口锁死。
        try:
            self.grab_set()
        except Exception:
            pass
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

        if auto:
            self._try_auto_login()
        else:
            # 用户手动登出后回来：不清空已保存的地址/账号，只清空口令焦点
            self.e_pass.focus_set()

    # ------------------------------------------------------------ 布局
    def _set_icon(self):
        try:
            import sys
            base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            ico = os.path.join(base, "app.ico")
            if os.path.exists(ico):
                self.after(200, lambda: self.iconbitmap(ico))
        except Exception:
            pass

    def _fit(self):
        """按实际内容高度定窗口，且永不超过屏幕可用高度。

        之前写死 520 + resizable(False,False)：高 DPI / 大字号 / 小屏笔记本上
        内容比窗口高，底部按钮和说明被直接裁掉，且窗口不能拉大也滚不动。
        现在内容放进可滚动容器，窗口高度取「内容所需」与「屏幕可用」的较小值；
        即使小到放不下，滚动条也能把下面全部露出来。
        """
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        try:
            need = int(self.winfo_reqheight()) + 20
        except Exception:
            need = self.H
        need = max(self.H, need)
        max_h = max(360, sh - 100)
        h = min(need, max_h)
        x = max(0, (sw - self.W) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{self.W}x{h}+{x}+{y}")

    def _build(self):
        c = theme.colors()
        sp, fs, rd, sz = ds.SP, ds.FS, ds.RD, ds.SZ

        # 外层：留白遵循 8pt，内容区左右各 32。
        # 用可滚动容器包住全部内容 —— 窗口再小也不会把底部截掉。
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)
        content = self.scroll

        # ---- 头部：品牌区居中，建立第一视觉层级
        head = ctk.CTkFrame(content, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(sp["xl"], sp["l"]))
        ctk.CTkLabel(head, text="☁", font=theme.font(38),
                     text_color=c["accent"]).pack()
        ctk.CTkLabel(head, text="MCSync", font=theme.font(24, "bold"),
                     text_color=c["text"]).pack(pady=(sp["xs"], 0))
        ctk.CTkLabel(head, text=t("Minecraft 存档云同步"), font=theme.font(fs["sub"]),
                     text_color=c["muted"]).pack()

        # ---- 表单卡片
        card = ctk.CTkFrame(content, fg_color=c["surface"], corner_radius=rd["card"],
                            border_width=1, border_color=c["line"])
        card.grid(row=1, column=0, sticky="ew", padx=sp["xl"], pady=(0, sp["m"]))
        card.grid_columnconfigure(0, weight=1)

        row = 0
        def field(label: str, placeholder: str, show: str = "", value: str = ""):
            nonlocal row
            ctk.CTkLabel(card, text=label, font=theme.font(fs["sub"]),
                         text_color=c["text2"], anchor="w").grid(
                row=row, column=0, sticky="w", padx=sp["m"], pady=(sp["m"], 0))
            row += 1
            e = ctk.CTkEntry(card, height=sz["entry_h"], font=theme.font(fs["body"]),
                             placeholder_text=placeholder, show=show,
                             fg_color=c["surface2"], border_color=c["line"],
                             corner_radius=rd["ctl"])
            e.grid(row=row, column=0, sticky="ew", padx=sp["m"], pady=(sp["xxs"], 0))
            if value:
                e.insert(0, value)
            row += 1
            return e

        self.e_url = field(t("服务器地址"), "http://1.2.3.4:36854",
                           value=str(cfg.get("server_url", "") or ""))
        self.e_user = field(t("账号"), t("管理员分配的账号"),
                            value=str(cfg.get("username", "") or ""))
        saved_pw = cfg.get_secret("password") or ""
        self.e_pass = field(t("口令"), "••••••••", show="●", value=saved_pw)

        for e in (self.e_url, self.e_user, self.e_pass):
            e.bind("<Return>", lambda _e: self._do_login())

        # ---- 记住我
        self.v_remember = ctk.BooleanVar(value=bool(cfg.get("remember_password", True)))
        ctk.CTkCheckBox(card, text=t("记住口令，下次自动登录"),
                        variable=self.v_remember, font=theme.font(fs["sub"]),
                        fg_color=c["accent"], border_color=c["line_hi"],
                        text_color=c["text2"]).grid(
            row=row, column=0, sticky="w", padx=sp["m"], pady=(sp["m"], 0))
        row += 1

        # ---- 主按钮：全宽、最高视觉权重
        self.b_login = ctk.CTkButton(card, text=t("登录"), height=sz["btn_h_lg"],
                                     font=theme.font(fs["body"], "bold"),
                                     fg_color=c["accent"], hover_color=c["accent_h"],
                                     text_color=c["accent_fg"], corner_radius=rd["ctl"],
                                     command=self._do_login)
        self.b_login.grid(row=row, column=0, sticky="ew", padx=sp["m"], pady=(sp["m"], sp["m"]))
        row += 1

        # ---- 状态行
        self.l_msg = ctk.CTkLabel(card, text="", font=theme.font(fs["cap"]),
                                  text_color=c["danger"], wraplength=self.W - 96,
                                  justify="left")
        self.l_msg.grid(row=row, column=0, sticky="w", padx=sp["m"], pady=(0, sp["m"]))
        row += 1

        # 自动登录卡在失效的旧服务器上时，用户必须有立刻夺回控制权的入口。
        # 这个按钮不随 _set_busy 禁用，地址框也始终可编辑。
        self.b_cancel_auto = ctk.CTkButton(
            card, text=t("停止自动登录，修改地址"), height=sz["btn_h_sm"],
            font=theme.font(fs["cap"]), fg_color="transparent",
            border_width=1, border_color=c["line"], text_color=c["text2"],
            command=self._cancel_auto)
        self.b_cancel_auto.grid(row=row, column=0, sticky="ew",
                                padx=sp["m"], pady=(0, sp["m"]))
        try:
            self.b_cancel_auto.grid_remove()
        except Exception:
            pass
        row += 1

        # ---- 底部：离线也能用
        foot = ctk.CTkFrame(content, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=sp["xl"])
        ctk.CTkLabel(foot, text=t("不登录也能管理本地存档，仅云同步功能不可用"),
                     font=theme.font(fs["cap"]), text_color=c["muted"],
                     wraplength=self.W - 64, justify="center").pack()
        ctk.CTkButton(foot, text=t("稍后，先本地使用"), height=sz["btn_h_sm"],
                      font=theme.font(fs["sub"]), fg_color="transparent",
                      border_width=1, border_color=c["line"], text_color=c["text2"],
                      command=self._skip).pack(pady=(sp["xs"], 0))

    # ------------------------------------------------------------ 行为
    def _try_auto_login(self):
        """有保存的凭据就后台自动登录，成功直接进主界面。"""
        url = str(cfg.get("server_url", "") or "").strip()
        user = str(cfg.get("username", "") or "").strip()
        pw = cfg.get_secret("password") or ""
        if not (url and user and pw) or not cfg.get("remember_password", True):
            return
        if not cfg.get("auto_login", True):
            return
        self._auto_tried = True
        self._auto_gen += 1
        gen = self._auto_gen
        self._set_busy(True, auto=True)
        self.l_msg.configure(text=t("正在自动登录…"),
                             text_color=theme.colors()["muted"])
        # quick=True：旧服务器已失效时 2.5 秒连接超时即返回，绝不把窗口
        # 卡成"点不动"。用户仍可随时点「停止自动登录」抢回控制权。
        threading.Thread(target=self._login_worker,
                         args=(url, user, pw, True, True, gen),
                         daemon=True).start()

    def _do_login(self):
        if self._busy:
            return
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pass.get()
        if not url or not user or not pw:
            self._msg(t("请填写服务器地址、账号与口令"), True)
            return
        self._auto_gen += 1
        gen = self._auto_gen
        self._set_busy(True)
        threading.Thread(target=self._login_worker,
                         args=(url, user, pw, False, True, gen),
                         daemon=True).start()

    def _login_worker(self, url: str, user: str, pw: str, is_auto: bool,
                      quick: bool = False, gen: int = 0):
        """登录实际耗时在网络，放在子线程，结果回投主线程。"""
        err = ""
        try:
            from core.api import Api
            # 不在后台线程写 server_url：用户可能正在把地址改成新服务器，
            # 旧线程回来一写就把新地址覆盖掉了。地址只在 _login_done
            # 成功分支里、以真正登录成功的那个地址为准落盘。
            api = Api(url)
            api.login(user, pw, quick=quick)
            self.result = {"url": url, "user": user, "api": api}
        except Exception as e:
            err = str(e)
        self.after(0, lambda: self._login_done(err, is_auto, user, pw, gen))

    def _login_done(self, err: str, is_auto: bool, user: str, pw: str,
                    gen: int = 0):
        # 用户点了「停止自动登录」或又发起了一次登录：丢弃过期结果
        if gen and gen != self._auto_gen:
            return
        self._set_busy(False)
        if err:
            if is_auto:
                # 旧服务器下线/换 IP 是最典型的场景。不能只清空提示，
                # 要明确告诉用户「去改地址」，并把焦点放到地址框。
                url = str(cfg.get("server_url", "") or "").strip()
                self.l_msg.configure(
                    text=t("无法连接原服务器") + (f" {url}" if url else "") +
                         "，" + t("请在上方修改服务器地址后重新登录"),
                    text_color=theme.colors()["warn"])
                self.e_url.focus_set()
            else:
                self._msg(err, True)
            return

        # 成功：按"记住我"决定是否保存口令
        cfg.set("username", user)
        # 以「真正登录成功的那个地址」为准，不用地址框里的当前值：
        # 自动登录期间用户可能已经在编辑新地址，两者混写会导致
        # 配置指向新服务器、Api 却连着旧服务器。
        cfg.set("server_url", self.result["url"])
        if self.v_remember.get():
            cfg.set_secret(pw, "password")
            cfg.set("remember_password", True)
        else:
            cfg.set_secret("", "password")
            cfg.set("remember_password", False)
        cfg.set("logged_in", True)
        self._finish()

    def _msg(self, text: str, is_err: bool):
        c = theme.colors()
        self.l_msg.configure(text=text,
                             text_color=c["danger"] if is_err else c["ok"])

    def _set_busy(self, busy: bool, auto: bool = False):
        self._busy = busy
        self.b_login.configure(state="disabled" if busy else "normal",
                               text=t("登录中…") if busy else t("登录"))
        try:
            if busy and auto:
                self.b_cancel_auto.grid()
            else:
                self.b_cancel_auto.grid_remove()
        except Exception:
            pass

    def _cancel_auto(self):
        """立刻放弃正在等待的自动登录，把控制权还给用户改地址。"""
        self._auto_gen += 1
        self._set_busy(False)
        self.l_msg.configure(
            text=t("已停止自动登录。请修改服务器地址后点「登录」"),
            text_color=theme.colors()["muted"])
        self.e_url.focus_set()

    def _skip(self):
        self.result = None
        cfg.set("logged_in", False)
        self._finish()

    def _on_close(self):
        self.result = None
        self._finish()

    def _finish(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.withdraw()
        cb, res = self.on_success, self.result
        # 延时销毁，避免在回调里销毁自己导致 Tk 报错
        self.after(60, self.destroy)
        if cb:
            cb(res)
