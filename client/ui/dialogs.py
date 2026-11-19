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

"""对话框：设置 / 冲突解决 / 版本管理 / 通用确认

═══════════════════════════════════════════════════════════════════════
  统一方案（customtkinter 6.0.0，全项目唯一 UI 标准）
═══════════════════════════════════════════════════════════════════════

  · 只用 CTk 原生控件。不混用 tkinter 原生控件、不 monkey patch、
    不探测版本、不降级、不重建"兼容窗口"。
  · 版本由 requirements / build.py 钉死 6.0.0，运行时不做任何版本判断。
  · ★ 绝不覆盖 tkinter 保留属性：``_w`` 是 widget path。
    旧版把 ``self._w = 720`` 存宽高，直接把窗口路径改成数字，
    Tcl 随即报 ``bad window path name "720"``，所有控件构造失败 ——
    这才是连续几版「窗口打不开 / 弹提示框 / 按钮消失」的总根因。
    宽高一律用 ``_dlg_w`` / ``_dlg_h``。
  · 一个 _BaseDialog 底座统一负责：布局顺序、动态换行、尺寸自适应、
    模态生命周期。各对话框只往 body / footer 放控件，不自己管窗口。

布局三条硬约束（Tk 按 pack 顺序分配空间，顺序错了底栏会被挤出可视区）：
  1) footer 先 pack(side="bottom")，永远占住底部
  2) body 后 pack(fill="both", expand=True)，只拿剩余空间；内容多时可滚
  3) 构造完按内容自适应尺寸，wraplength 跟随窗口宽度，不写死像素
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from core.config import cfg
from core.protocol import DEFAULT_IGNORE_RULES, human_size
from ui import theme
from core.i18n import t
from core import i18n


def _as_int(v, default: int = 0) -> int:
    """尺寸值统一转 int：winfo_* 与配置值可能是 str / float / None。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if v is None:
        return default
    try:
        import re
        m = re.match(r"^[-+]?\d+", str(v).strip())
        return int(m.group(0)) if m else default
    except Exception:
        return default


class _BaseDialog(ctk.CTkToplevel):
    """全部对话框的公共底座（CTk 6.0 原生实现）。

    没有 __init_subclass__ 包装、没有异常"应急窗口"、没有 grab 花活、
    没有原生 tkinter 降级。构造就是构造，失败就让它失败并由调用方处理 ——
    层层兜底反而会把一个真实错误伪装成十几种不同症状。
    """

    def __init__(self, master, title: str, width: int = 720, height: int = 560,
                 scroll: bool = True, footer_pad: int = 14):
        super().__init__(master)
        self.master_window = master
        self.result = None
        self._closed = False
        self._grabbed = False
        self._init_errors: List[str] = []
        self._last_error = None
        self._wrap_labels: List[tuple] = []
        self._wrap_pending = False
        self._scroll = scroll

        # ★ 注意：self._w / self._h 是 tkinter 内部属性，绝不能赋值。
        self._dlg_w = _as_int(width, 720)
        self._dlg_h = _as_int(height, 560)
        w0, h0 = self._dlg_w, self._dlg_h

        c = theme.colors()
        self.title(title)
        self.configure(fg_color=c.get("card") or c.get("surface") or "#24242a")
        self.resizable(True, True)
        self.transient(master)
        self.deiconify()
        self.lift()

        # ① footer 先 pack —— 先占住底部，永远在可视区
        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.pack(side="bottom", fill="x", padx=footer_pad,
                         pady=(0, footer_pad))

        # ② body 后 pack，只拿剩余空间
        if scroll:
            self.body = ctk.CTkScrollableFrame(
                self, fg_color="transparent", corner_radius=0)
            self.body.pack(fill="both", expand=True)
            self.body.grid_columnconfigure(0, weight=1)
        else:
            self.body = self

        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._center(w0, h0)
        self.after_idle(self._fit_content)
        # 窗口 map 之后再抓 grab；抓不到就退化为 transient，不锁死主窗口
        self.after_idle(self._arm_modal)

    # ------------------------------------------------------------ 模态
    def _arm_modal(self):
        try:
            if not self._closed:
                self.grab_set()
                self._grabbed = True
        except Exception:
            self._grabbed = False

    def _ok(self):
        """默认确认：子类通常会覆写。"""
        self.result = True
        self._close()

    def _close(self):
        """幂等关闭：grab 一定释放、窗口一定销毁、wait_window 一定返回。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self._grabbed = False
        try:
            self.destroy()
        except Exception:
            pass

    def wait_window(self):
        """进入模态前保证底栏至少有一对可点按钮（纯 CTk 组件，非降级）。"""
        try:
            if not self.footer.winfo_children():
                self._ensure_footer_buttons(self.footer, [
                    (t("取消"), self._close),
                    (t("确定"), getattr(self, "_ok", self._close)),
                ])
        except Exception:
            pass
        return super().wait_window()

    def _ensure_footer_buttons(self, bar, specs):
        """底栏为空时补建按钮 —— 统一用 CTkButton，保持风格一致。"""
        try:
            kids = list(bar.winfo_children())
        except Exception:
            kids = []
        if kids:
            return 0
        c = theme.colors()
        made = 0
        for label, cmd in reversed(list(specs)):
            ctk.CTkButton(bar, text=str(label), width=100, height=34,
                          font=theme.font(12),
                          fg_color=c.get("accent"),
                          hover_color=c.get("accent_h"),
                          text_color=c.get("accent_fg"),
                          command=cmd).pack(side="right", padx=(8, 0))
            made += 1
        return made

    # ------------------------------------------------------------ 尺寸
    def _center(self, w, h):
        try:
            self.update_idletasks()
            sw = _as_int(self.winfo_screenwidth(), 1920)
            sh = _as_int(self.winfo_screenheight(), 1080)
            w = min(_as_int(w, 720), max(360, sw - 80))
            h = min(_as_int(h, 560), max(280, sh - 120))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.minsize(min(360, w), min(220, h))
        except Exception:
            try:
                self.geometry("720x560")
            except Exception:
                pass

    def _fit_content(self):
        """按内容把窗口撑到「刚好装得下」；宽高都受屏幕上限约束。"""
        try:
            self._apply_wrap()
            self.update_idletasks()
            sw = _as_int(self.winfo_screenwidth(), 1920)
            sh = _as_int(self.winfo_screenheight(), 1080)
            max_w, max_h = max(360, sw - 80), max(280, sh - 120)

            need_w = _as_int(self.winfo_reqwidth(), 360) + 24
            need_h = max(_as_int(self.winfo_reqheight(), 240),
                         self._content_height()) + 24
            cur_w = _as_int(self.winfo_width(), 720)
            cur_h = _as_int(self.winfo_height(), 560)

            # 底栏硬下限：按钮 34px + 内边距 + 余量，防止窗口按"没有按钮"定型
            footer_h = max(_as_int(self.footer.winfo_reqheight(), 34),
                           34 + 2 * 14 + 8)
            need_h = max(need_h, self._body_height() + footer_h + 48)

            w = min(max(cur_w, need_w, 360), max_w, 1100)
            h = min(max(cur_h, need_h, 240), max_h)
            if w != cur_w or h != cur_h:
                self.geometry(f"{w}x{h}")
            self.minsize(min(360, w), min(220, h))
            self._apply_wrap()

            # 运行时可见性校验：直接看 footer 是不是真在窗口里
            for _ in range(3):
                try:
                    self.update_idletasks()
                    wh = _as_int(self.winfo_height(), 0)
                    fy = _as_int(self.footer.winfo_y(), -1)
                    fh = max(_as_int(self.footer.winfo_height(), 0), 34)
                    if wh <= 0 or fy < 0 or fy + fh <= wh - 4:
                        break
                    grow = min(max_h - wh, fy + fh + 20 - wh)
                    if grow <= 0:
                        break
                    h = min(wh + grow, max_h)
                    self.geometry(f"{w}x{h}")
                except Exception:
                    break
        except Exception:
            pass

    def _body_height(self) -> int:
        """按登记标签的文本长度估行数，不依赖 CTkScrollableFrame.winfo_children()。"""
        win_w = _as_int(self.winfo_width(), 720)
        total = 0
        for lbl, pad in self._wrap_labels:
            try:
                wrap = max(160, win_w - _as_int(pad, 48) - 24)
                txt = str(lbl.cget("text") or "")
            except Exception:
                txt, wrap = "", 420
            per = max(16, wrap // 11)
            lines = 0
            for seg in txt.split("\n"):
                lines += max(1, (len(seg) + per - 1) // per) if seg else 1
            by_text = 16 + max(1, lines) * 22 + 12
            try:
                by_req = _as_int(lbl.winfo_reqheight(), 0)
            except Exception:
                by_req = 0
            total += max(by_text, by_req) + 8
        return total

    def _content_height(self) -> int:
        total = 0
        hosts = [self.body] if (self._scroll and self.body is not self) else [self]
        try:
            for host in hosts:
                if not hasattr(host, "winfo_children"):
                    continue
                for ch in host.winfo_children():
                    if ch is self.footer:
                        continue
                    try:
                        total += _as_int(ch.winfo_reqheight(), 24) + 8
                    except Exception:
                        total += 24
            total += _as_int(self.footer.winfo_reqheight(), 34)
        except Exception:
            pass
        return total + 32

    # ------------------------------------------------------------ 动态换行
    def auto_wrap(self, label, pad: int = 48):
        """注册标签，wraplength 跟随窗口宽度，不写死像素。"""
        self._wrap_labels.append((label, pad))
        try:
            label.configure(wraplength=420)
        except Exception:
            pass
        self.after_idle(self._apply_wrap)
        return label

    def _on_configure(self, e):
        if e.widget is not self or self._wrap_pending:
            return
        self._wrap_pending = True
        self.after(80, self._do_wrap)

    def _do_wrap(self):
        self._wrap_pending = False
        self._apply_wrap()

    def _apply_wrap(self):
        try:
            win_w = _as_int(self.winfo_width(), 720)
            for lbl, pad in self._wrap_labels:
                lbl.configure(wraplength=max(160, win_w - _as_int(pad, 48) - 24))
        except Exception:
            pass

    # ------------------------------------------------------------ 常驻底栏
    def make_footer(self, pad: int = 14) -> ctk.CTkFrame:
        """返回 __init__ 里已 pack(side=bottom) 的底栏容器。

        用 ``before=self.body`` 显式把 footer 钉在 body 之前，不依赖注册
        顺序；按钮填完后再排一次 _fit_content 按"按钮已就位"重新量高度。
        """
        pad = _as_int(pad, 14)
        opts = dict(side="bottom", fill="x", padx=pad, pady=(0, pad))
        try:
            if self._scroll and self.body is not self:
                opts["before"] = self.body
            self.footer.pack_configure(**opts)
        except Exception:
            try:
                self.footer.pack_configure(padx=pad, pady=(0, pad))
            except Exception:
                pass
        try:
            self.after_idle(self._fit_content)
        except Exception:
            pass
        return self.footer



class ConfirmDialog(_BaseDialog):
    """通用确认框：正文 + 附注 + 取消/确认 两个按钮（纯 CTk 6.0）。"""

    def __init__(self, master, title: str, message: str, ok_text: str = "确定",
                 danger: bool = False, extra: Optional[str] = None):
        super().__init__(master, title, 520, 260)
        self.result = False
        c = theme.colors()
        line = c.get("line") or c.get("border") or "#3a3a44"
        text_c = c.get("text") or "#ececf1"
        muted = c.get("muted") or c.get("text2") or "#90909a"
        danger_c = c.get("danger") or "#b91c1c"

        self.auto_wrap(ctk.CTkLabel(self.body, text=message,
                                    font=theme.font(13),
                                    justify="left"), pad=60).pack(
            anchor="w", padx=20, pady=(24, 8))
        if extra:
            self.auto_wrap(ctk.CTkLabel(self.body, text=extra,
                                        font=theme.font(11),
                                        text_color=muted,
                                        justify="left"),
                           pad=60).pack(anchor="w", padx=20, pady=(0, 8))

        bar = self.make_footer(20)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34,
                      fg_color="transparent", border_width=1,
                      border_color=line, text_color=text_c,
                      font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=str(ok_text), width=110, height=34,
                      font=theme.font(12, "bold"),
                      fg_color=danger_c if danger else c.get("accent"),
                      hover_color="#b91c1c" if danger else c.get("accent_h"),
                      text_color=c.get("accent_fg"),
                      command=self._ok).pack(side="right")
        self.wait_window()

    def _ok(self):
        self.result = True
        self._close()



class ConflictDialog(_BaseDialog):
    """
    返回 {path: 'local'|'cloud'|'both'}，取消返回 None
    both = 保留云端副本（另存为 _冲突副本/），同时上传/下载本地版本
    """

    def __init__(self, master, conflicts: List[dict], direction: str = "upload"):
        super().__init__(master, "解决同步冲突", 640, 520, scroll=False)
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
        self.auto_wrap(ctk.CTkLabel(head, text=tip, font=theme.font(11),
                                    text_color=c["muted"], justify="left"),
                       pad=48).pack(anchor="w", pady=(2, 0))

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
        bar = self.make_footer(16)
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
        self.auto_wrap(ctk.CTkLabel(row, text=cf.get("path", ""),
                                    font=theme.font(12, "bold"), anchor="w",
                                    justify="left"), pad=180).grid(
            row=0, column=0, sticky="w")
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
        super().__init__(master, f"版本管理 · {cloud_name}", 620, 520, scroll=False)
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
        super().__init__(master, "设置", 720, 640, scroll=False)
        self.on_saved = on_saved
        c = theme.colors()
        self.tab = ctk.CTkTabview(self, fg_color=c["card2"])
        self.tab.pack(fill="both", expand=True, padx=14, pady=14)

        # 每个标签页内容放进可滚动容器。
        # 旧版直接用 tab 本体做 grid / pack，连接页有 15 行控件 + 说明文字，
        # 总高超过窗口，底部「保存 / 取消」和最后几项被直接裁掉。
        # 现在窗口再小也能滚到底，且按钮常驻在窗口底部不随内容滚走。
        pages = {}
        for name in ("连接", "同步", "忽略规则", "高级", "关于"):
            tab = self.tab.add(name)
            tab.grid_rowconfigure(0, weight=1)
            tab.grid_columnconfigure(0, weight=1)
            sc = ctk.CTkScrollableFrame(tab, fg_color="transparent")
            sc.grid(row=0, column=0, sticky="nsew")
            sc.grid_columnconfigure(1, weight=1)
            pages[name] = sc
        self._build_conn(pages["连接"])
        self._build_sync(pages["同步"])
        self._build_ignore(pages["忽略规则"])
        self._build_adv(pages["高级"])
        self._build_about(pages["关于"])
        self._register_wraps()

        # 保存/取消常驻在窗口底部，不随任一标签页的内容滚走
        bar = self.make_footer(14)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"], font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("保存"), width=100, height=34, font=theme.font(12, "bold"),
                      command=self._save).pack(side="right")

    def _register_wraps(self):
        """把本对话框里所有写死了 wraplength 的说明文字改成动态换行。

        基准窗口 720：pad = 720 - 原 wraplength，于是原尺寸下与旧版观感一致，
        窗口被夹窄时文字跟着收拢，不会再从右侧被裁掉。
        """
        seen = set()

        def walk(w):
            try:
                if id(w) in seen:
                    return
                seen.add(id(w))
                cur = int(w.cget("wraplength") or 0)
            except Exception:
                cur = 0
            # 统一按「窗口宽 - 84」换行；不再从写死的初值反推 pad，
            # 否则初值一改就跟着变，等于换个地方写死像素。
            already = any(lbl is w for lbl, _ in self._wrap_labels)
            if cur > 100 and not already:
                self.auto_wrap(w, pad=60)
            try:
                for ch in w.winfo_children():
                    walk(ch)
            except Exception:
                pass

        walk(self)

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
                     font=theme.font(10), text_color=c["muted"], wraplength=420,
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
                     font=theme.font(10), text_color=c["muted"], wraplength=420,
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
                     font=theme.font(10), text_color=c["muted"], wraplength=420,
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
        self.e_depth.insert(0, str(cfg.get("scan_depth", 6)))
        ctk.CTkLabel(f, text=t("范围 1~12，默认 6；整合包放得深就调大，越大越慢"),
                     font=theme.font(10), text_color=c["muted"],
                     wraplength=420, justify="left").grid(
            row=r, column=1, sticky="w", padx=110)
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
                     font=theme.font(10), text_color=c["muted"], wraplength=420,
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

        # ---- 定时同步（默认关闭）
        # 这段控件曾被误粘到文件末尾 InputDialog._ok 里，成了永不执行的死代码；
        # 而 _save() 又无条件读取 v_auto / e_auto / v_running，于是点「保存」
        # 会抛 AttributeError 导致整个设置保存不了。这里补回正确的位置。
        r2 = r + 3
        ctk.CTkLabel(f, text=t("定时同步"), font=theme.font(12, "bold")).grid(
            row=r2, column=0, sticky="w", padx=14, pady=(12, 4))
        asf = ctk.CTkFrame(f, fg_color="transparent")
        asf.grid(row=r2, column=0, sticky="w", padx=14, pady=(0, 4))
        self.v_auto = ctk.BooleanVar(value=bool(cfg.get("auto_sync_enabled", False)))
        ctk.CTkSwitch(asf, text=t("开启（默认关闭）"), variable=self.v_auto,
                      font=theme.font(12), progress_color=c["accent"]).pack(side="left")
        self.e_auto = ctk.CTkEntry(asf, font=theme.font(12), width=60, height=30)
        self.e_auto.pack(side="left", padx=(10, 0))
        self.e_auto.insert(0, str(cfg.get("auto_sync_minutes", 30)))
        ctk.CTkLabel(asf, text=t("分钟一次（最少 5）"), font=theme.font(10),
                     text_color=c["muted"]).pack(side="left", padx=(6, 0))
        self.auto_wrap(ctk.CTkLabel(
            f, text=t("开启后按间隔自动上传当前选中的存档；关闭窗口即停止。"),
            font=theme.font(10), text_color=c["muted"], justify="left"),
            pad=80).grid(row=r2 + 1, column=0, sticky="w", padx=14)

        self.v_running = ctk.BooleanVar(value=bool(cfg.get("allow_sync_while_running", False)))
        ctk.CTkSwitch(f, text=t("允许在游戏运行时同步（可能读到半截数据，不建议）"),
                      variable=self.v_running, font=theme.font(12),
                      progress_color=c["accent"]).grid(
            row=r2 + 2, column=0, sticky="w", padx=14, pady=(10, 6))

    # ------------------------------------------------------------ 忽略
    def _build_ignore(self, f):
        c = theme.colors()
        ctk.CTkLabel(f, text=t("在默认规则之外追加的规则（glob 语法，一行一条）"),
                     font=theme.font(12), wraplength=420, justify="left").pack(anchor="w", padx=14, pady=(12, 4))
        self.ig_box = ctk.CTkTextbox(f, font=theme.font(11), height=180)
        self.ig_box.pack(fill="both", expand=True, padx=14, pady=4)
        self.ig_box.insert("1.0", "\n".join(cfg.get("ignore_rules", []) or []))
        ctk.CTkLabel(f, text="默认已忽略：" + "、".join(DEFAULT_IGNORE_RULES[:8]) + " 等",
                     font=theme.font(10), text_color=c["muted"], wraplength=420, justify="left").pack(
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

        # 旧版把「界面语言」也塞在 row=1，靠 padx=(180,14) 硬错开，
        # 高 DPI / 大字号下会与「外观点」重叠甚至盖住下拉框。改为独占一行。
        ctk.CTkLabel(f, text=t("界面语言"), font=theme.font(12)).grid(row=2, column=0, sticky="w", padx=14, pady=12)
        self.m_lang = ctk.CTkOptionMenu(f, values=list(i18n.LANGUAGES.values()), width=120, font=theme.font(12))
        self.m_lang.grid(row=2, column=1, sticky="w", padx=14, pady=12)
        _cur = str(cfg.get("language", "zh") or "zh")
        self.m_lang.set(i18n.LANGUAGES.get(_cur, i18n.LANGUAGES["zh"]))

        ctk.CTkLabel(f, text=t("本地缓存"), font=theme.font(12)).grid(row=3, column=0, sticky="w", padx=14, pady=12)
        btns = ctk.CTkFrame(f, fg_color="transparent")
        btns.grid(row=3, column=1, sticky="w", padx=14, pady=12)
        ctk.CTkButton(btns, text=t("清理块缓存"), width=110, height=30, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"], text_color=c["text"],
                      command=self._clear_cache).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text=t("打开配置目录"), width=110, height=30, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"], text_color=c["text"],
                      command=self._open_dir).pack(side="left")
        ctk.CTkLabel(f, text=f"配置与缓存目录：{cfg.dir}", font=theme.font(10),
                     text_color=c["muted"], wraplength=420, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=14, pady=4)

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
                         wraplength=420).pack(anchor="w", padx=16, pady=2)

    # ------------------------------------------------------------ 保存
    def _repair(self):
        """用账号口令重新登录（丢弃旧私钥，重新登记）。"""
        from core.api import Api
        cfg.set("server_url", self.e_url.get().strip())
        cfg.set("username", self.e_user.get().strip())
        cfg.set_secret(self.e_pass.get().strip(), "password")
        cfg.set_secret("")
        cfg.set("paired_server", "")
        # 必须放后台：同步跑时若填的还是失效的旧地址，设置窗口会整个冻住，
        # 用户连把地址改成新服务器都做不到。quick=True 保证几秒内返回。
        import threading

        def work():
            api = Api(cfg.server_url)
            api.login(cfg.get("username", ""), cfg.get_secret("password"),
                      quick=True)
            return api.me or {}

        def finish(who=None, err=None):
            if err:
                self._toast(f"登录失败：{err}")
                return
            role = "管理员" if who.get("role") == "admin" else "普通用户"
            self._toast(f"登录成功：{who.get('display') or cfg.get('username','')}（{role}）")

        def runner():
            try:
                who = work()
                self.after(0, lambda: finish(who=who))
            except Exception as e:
                self.after(0, lambda err=e: finish(err=err))

        threading.Thread(target=runner, daemon=True).start()

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
            from core.scanner import MAX_SCAN_DEPTH, DEFAULT_SCAN_DEPTH
            cfg.set("scan_depth", max(1, min(MAX_SCAN_DEPTH,
                                            int(self.e_depth.get().strip()
                                                or str(DEFAULT_SCAN_DEPTH)))))
        except (ValueError, ImportError):
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
class _InstancePickDialog(_BaseDialog):
    """选择云端存档所属整合包（可下拉选已有实例，也可直接输入新名字）。"""

    def __init__(self, master, save_name: str, current: str, choices: List[str]):
        super().__init__(master, "归类到整合包", 480, 300)
        self.result: Optional[str] = None
        c = theme.colors()

        self.auto_wrap(ctk.CTkLabel(self.body, text=f"云端存档：{save_name}",
                                         font=theme.font(12, "bold"),
                                         justify="left", anchor="w"), pad=60).pack(anchor="w", padx=20, pady=(20, 4))
        self.auto_wrap(ctk.CTkLabel(self.body, text=t("选择它所属的整合包（实例）。留空表示归入「未分类」。"),
                                         font=theme.font(10), text_color=c["muted"],
                                         justify="left", anchor="w"), pad=60).pack(anchor="w", padx=20, pady=(0, 12))

        opts = ["（留空 · 未分类）"] + [x for x in choices if x]
        self.m = ctk.CTkOptionMenu(self.body, values=opts, font=theme.font(12), height=34,
                                   fg_color=c["card"], button_color=c["accent"],
                                   button_hover_color=c["accent_hover"])
        self.m.pack(fill="x", padx=20)
        self.m.set(current if current and current in opts else opts[0])

        ctk.CTkLabel(self.body, text=t("或直接输入整合包名："), font=theme.font(10),
                     text_color=c["muted"], anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        self.e = ctk.CTkEntry(self.body, font=theme.font(12), height=34,
                              placeholder_text=t("留空则使用上面的下拉选择"))
        self.e.pack(fill="x", padx=20)
        self.e.bind("<Return>", lambda ev: self._ok())

        bar = self.make_footer(20)
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

        self.auto_wrap(ctk.CTkLabel(self.body, text=f"云端存档：{save_name}",
                                         font=theme.font(13, "bold"), anchor="w"), pad=60).pack(anchor="w", padx=20, pady=(18, 2))

        if instance and default_dir:
            tip = (f"它来自整合包「{instance}」，本机已找到同名实例，"
                   f"默认装到该实例的存档目录。")
        elif instance:
            tip = (f"它来自整合包「{instance}」，但本机没有同名实例。"
                   f"请手动选择要装到哪个实例的存档目录。")
        else:
            tip = "该云端存档没有记录整合包信息，请手动选择保存位置。"
        self.auto_wrap(ctk.CTkLabel(self.body, text=tip, font=theme.font(10),
                                         text_color=c["muted"], anchor="w",
                                         justify="left"), pad=60).pack(
            anchor="w", padx=20, pady=(0, 12))

        if instance_names:
            ctk.CTkLabel(self.body, text=t("装到这个实例："), font=theme.font(11),
                         anchor="w").pack(anchor="w", padx=20, pady=(0, 4))
            self.m = ctk.CTkOptionMenu(
                self.body, values=instance_names, font=theme.font(12), height=34,
                fg_color=c["card"], button_color=c["accent"],
                button_hover_color=c["accent_hover"], command=lambda _v: self._sync())
            self.m.pack(fill="x", padx=20)
            if instance in instance_names:
                self.m.set(instance)

        ctk.CTkLabel(self.body, text=t("保存为（存档文件夹名）："), font=theme.font(11),
                     anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        self.e_name = ctk.CTkEntry(self.body, font=theme.font(12), height=34)
        self.e_name.pack(fill="x", padx=20)
        self.e_name.insert(0, save_name)

        ctk.CTkLabel(self.body, text=t("完整路径："), font=theme.font(11),
                     anchor="w").pack(anchor="w", padx=20, pady=(12, 4))
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", padx=20)
        row.grid_columnconfigure(0, weight=1)
        self.e_dir = ctk.CTkEntry(row, font=theme.font(11), height=34)
        self.e_dir.grid(row=0, column=0, sticky="ew")
        self.e_dir.insert(0, self.default_dir)
        ctk.CTkButton(row, text=t("浏览"), width=70, height=34, font=theme.font(11),
                      fg_color="transparent", border_width=1, border_color=c["line"],
                      text_color=c["text"], command=self._pick).grid(
            row=0, column=1, padx=(8, 0))

        bar = self.make_footer(20)
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
        self.auto_wrap(ctk.CTkLabel(self.body, text=label, font=theme.font(12),
                                         justify="left"), pad=60).pack(
            anchor="w", padx=20, pady=(22, 6))
        self.e = ctk.CTkEntry(self.body, font=theme.font(12), height=34)
        self.e.pack(fill="x", padx=20)
        self.e.insert(0, default)
        self.e.focus()
        self.e.bind("<Return>", lambda e: self._ok())
        bar = self.make_footer(20)
        ctk.CTkButton(bar, text=t("取消"), width=90, height=34, fg_color="transparent", border_width=1,
                      border_color=c["line"], text_color=c["text"], font=theme.font(12),
                      command=self._close).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text=t("确定"), width=100, height=34, font=theme.font(12, "bold"),
                      command=self._ok).pack(side="right")
        self.wait_window()

    def _ok(self):
        self.result = self.e.get().strip()
        self._close()
