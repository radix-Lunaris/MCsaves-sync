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

"""
「玩家数据」页 —— 精简版。

主路径已经在上传前自动完成（sync.py），不需要用户手动点任何按钮：
  上传 → 自动摘除 level.dat 内嵌的房主玩家数据 → 云端版本天然共享友好。
所以这里只留必要内容：
  · 自动处理状态与「我的 UUID」（可选）
  · 对**已经上传到云端的历史存档**做补修：下载 → 修正 → 生成新版本
  · 处理结果输出

处理云端存档时原来的历史版本仍然保留，随时可在管理页回滚。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import List, Optional

import customtkinter as ctk

from core import playerdata as pd
from core.config import cfg
from core.i18n import t
from ui import design as ds
from ui import theme


class PlayerDataPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        c = theme.colors()
        sp, fs, rd, sz = ds.SP, ds.FS, ds.RD, ds.SZ

        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._cloud: List[dict] = []
        self._busy = False

        # ---------------- 标题 ----------------
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=sp["s"], pady=(sp["m"], sp["xs"]))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=t("🪪 玩家数据"), font=theme.font(fs["h1"], "bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            head,
            text=t("上传时会自动摘除 level.dat 里内嵌的房主玩家数据，并归档成独立文件。"
                   "云端存档因此天然共享友好：别人下载后打开就是自己的角色，房主的数据也不会丢。"),
            font=theme.font(fs["sub"]), text_color=c["muted"], anchor="w",
            justify="left", wraplength=860).grid(row=1, column=0, sticky="w",
                                                 pady=(sp["xxs"], 0))

        # ---------------- 必要参数 ----------------
        opt = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["card2"],
                           border_width=1, border_color=c["line"])
        opt.grid(row=1, column=0, sticky="ew", padx=sp["s"], pady=sp["xs"])
        opt.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opt, text=t("我的 UUID（可选）"), font=theme.font(fs["sub"])).grid(
            row=0, column=0, sticky="w", padx=(sp["m"], sp["xs"]), pady=(sp["s"], sp["xxs"]))
        self.e_uuid = ctk.CTkEntry(opt, height=sz["entry_h"], font=theme.font(fs["sub"]),
                                   placeholder_text=t("留空即可，自动处理"))
        self.e_uuid.grid(row=0, column=1, sticky="ew", padx=(0, sp["m"]), pady=(sp["s"], sp["xxs"]))
        self.e_uuid.insert(0, str(cfg.get("my_uuid", "") or ""))

        ctk.CTkLabel(
            opt,
            text=t("填上后，若内嵌数据本就属于你则原样保留；留空一律归档+摘除。"),
            font=theme.font(fs["cap"]), text_color=c["muted"],
            anchor="w", justify="left", wraplength=860).grid(
            row=1, column=1, sticky="w", padx=(0, sp["m"]), pady=(0, sp["xxs"]))
        self.l_auto = ctk.CTkLabel(opt, text="", font=theme.font(fs["cap"]),
                                   text_color=c["ok"], anchor="w")
        self.l_auto.grid(row=2, column=1, sticky="w", padx=(0, sp["m"]), pady=(0, sp["s"]))
        self._refresh_auto_label()

        # ---------------- 云端存档补修 ----------------
        cloud = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["card2"],
                             border_width=1, border_color=c["line"])
        cloud.grid(row=2, column=0, sticky="ew", padx=sp["s"], pady=sp["xs"])
        cloud.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cloud, text=t("☁ 处理云端存档"), font=theme.font(fs["h2"], "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=sp["m"], pady=(sp["s"], sp["xxs"]))
        ctk.CTkLabel(
            cloud,
            text=t("历史版本里若仍带着房主数据，可在这里补修：拉取 → 修正 → 上传为新版本。"
                   "原版本保留可回滚，其他人重新下载即可。"),
            font=theme.font(fs["cap"]), text_color=c["muted"],
            anchor="w", justify="left", wraplength=860).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=sp["m"], pady=(0, sp["xs"]))

        self.cb_cloud = ctk.CTkComboBox(cloud, values=[], font=theme.font(fs["sub"]),
                                        height=sz["entry_h"], state="readonly")
        self.cb_cloud.grid(row=2, column=0, columnspan=2, sticky="ew",
                           padx=(sp["m"], sp["xs"]), pady=(0, sp["s"]))
        self.b_cloud_refresh = ctk.CTkButton(
            cloud, text=t("🔄"), width=sz["btn_h"], height=sz["entry_h"],
            font=theme.font(fs["body"]), fg_color="transparent", border_width=1,
            border_color=c["line"], text_color=c["text2"], command=self.load_cloud)
        self.b_cloud_refresh.grid(row=2, column=2, sticky="w", padx=(0, sp["m"]),
                                  pady=(0, sp["s"]))

        self.b_cloud = ctk.CTkButton(
            cloud, text=t("☁ 处理选中存档"), height=sz["btn_h"],
            font=theme.font(fs["body"], "bold"), fg_color=c["accent"],
            hover_color=c["accent_h"], command=self.do_cloud_fix)
        self.b_cloud.grid(row=3, column=0, sticky="w", padx=sp["m"], pady=(0, sp["s"]))
        self.l_prog = ctk.CTkLabel(cloud, text="", font=theme.font(fs["cap"]),
                                   text_color=c["muted"], anchor="w")
        self.l_prog.grid(row=3, column=1, columnspan=2, sticky="w",
                         padx=(sp["xs"], sp["m"]), pady=(0, sp["s"]))

        # ---------------- 输出 ----------------
        box = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["card2"],
                           border_width=1, border_color=c["line"])
        box.grid(row=3, column=0, sticky="nsew", padx=sp["s"], pady=sp["xs"])
        box.grid_rowconfigure(1, weight=1)
        box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(box, text=t("处理结果"), font=theme.font(fs["h2"], "bold")).grid(
            row=0, column=0, sticky="w", padx=sp["m"], pady=(sp["s"], sp["xxs"]))
        self.txt = ctk.CTkTextbox(box, font=theme.font(fs["sub"]),
                                  fg_color=c["surface"], text_color=c["text"],
                                  corner_radius=rd["ctl"], wrap="word")
        self.txt.grid(row=1, column=0, sticky="nsew", padx=sp["m"], pady=(0, sp["m"]))
        self._set_text(t("上传时自动处理已开启，通常无需在这里操作。"))

        note = ctk.CTkLabel(
            self,
            text=t("修改前自动备份 level.dat 到 .mcsync；处理完全幂等，房主数据归档后仍按自己 UUID 可读回。"),
            font=theme.font(fs["cap"]), text_color=c["muted"],
            anchor="w", justify="left", wraplength=900)
        note.grid(row=4, column=0, sticky="ew", padx=sp["s"], pady=(0, sp["m"]))

    # ------------------------------------------------------------ 工具
    def _set_text(self, s: str):
        try:
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", s)
        except Exception:
            pass

    def _append(self, s: str):
        try:
            self.txt.insert("end", s)
        except Exception:
            pass

    def _prog(self, s: str):
        try:
            self.l_prog.configure(text=s)
        except Exception:
            pass

    def _refresh_auto_label(self):
        on = bool(cfg.get("auto_fix_playerdata", True))
        try:
            self.l_auto.configure(
                text=(t("自动处理：上传前已开启，下载时兜底")
                      if on else t("自动处理：已关闭（设置 → 同步）")),
                text_color=theme.colors()["ok"] if on else theme.colors()["warn"])
        except Exception:
            pass

    def _my_uuid(self) -> str:
        try:
            return str(self.e_uuid.get() or "").strip()
        except Exception:
            return ""

    def _api(self):
        api = getattr(self.app, "api", None)
        if api is None:
            raise RuntimeError(t("请先登录服务器"))
        return api

    # ------------------------------------------------------------ 云端列表
    def on_show(self):
        self._refresh_auto_label()
        try:
            saved = str(cfg.get("my_uuid", "") or "")
            if self._my_uuid() != saved:
                self.e_uuid.delete("0", "end")
                self.e_uuid.insert(0, saved)
        except Exception:
            pass
        self.load_cloud()

    def load_cloud(self):
        if self._busy:
            return
        self._prog(t("正在读取云端列表…"))

        def work():
            return self._api().list_saves()

        def done(saves):
            self._cloud = [s for s in (saves or []) if (s.get("kind") or "save") == "save"]
            names = [str(s.get("name", "")) for s in self._cloud if s.get("name")]
            try:
                self.cb_cloud.configure(values=names)
                if names:
                    self.cb_cloud.set(names[0])
                else:
                    self.cb_cloud.set("")
            except Exception:
                pass
            self._prog(f"{t('云端存档')} {len(names)} {t('个')}")

        def fail(e):
            self._prog("")
            self._set_text(f"{t('读取云端列表失败')}：{e}")

        self.app.run_async(work, done, fail)

    # ------------------------------------------------------------ 云端补修
    def do_cloud_fix(self):
        if self._busy:
            return
        name = ""
        try:
            name = str(self.cb_cloud.get() or "").strip()
        except Exception:
            pass
        if not name:
            self._set_text(t("请先选择要处理的云端存档。"))
            return
        my = self._my_uuid()
        try:
            cfg.set("my_uuid", my)
        except Exception:
            pass

        from ui.dialogs import ConfirmDialog
        dlg = ConfirmDialog(
            self.app, t("二次确认 · 处理云端存档"),
            f"{t('即将处理云端存档')}「{name}」：\n\n"
            f"{t('· 拉取最新版本 → 摘除内嵌房主数据 → 上传为新版本')}\n"
            f"{t('· 原版本保留，可在管理页回滚')}\n"
            f"{t('· 其他人重新下载后即为共享友好版本')}\n\n"
            f"{t('确认继续吗？')}",
            ok_text=t("确认处理"), danger=False)
        if not getattr(dlg, "result", None):
            return

        self._busy = True
        self._prog(t("正在处理…"))
        self._set_text(f"⏳ {t('正在处理')}「{name}」…\n")

        def work():
            api = self._api()
            meta = next((s for s in self._cloud if str(s.get("name", "")) == name), {})
            tmp = tempfile.mkdtemp(prefix="mcsync-pd-cloud-")
            save_dir = os.path.join(tmp, name)
            try:
                from core.sync import SyncEngine
                eng = SyncEngine(api, log=lambda m: None)
                eng.download(save_dir, name, auto_fix_playerdata=False)
                r = pd.fix(save_dir, my)
                uploaded = None
                if r.changed:
                    uploaded = eng.upload(
                        save_dir, name,
                        instance=str(meta.get("instance", "") or ""),
                        kind=str(meta.get("kind", "") or "save"))
                return {"name": name, "fix": r, "upload": uploaded}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        def done(res):
            self._busy = False
            self._prog("")
            r = res["fix"]
            up = res.get("upload")
            lines = [f"☁ {res['name']}", ""]
            if r.changed:
                lines.append(f"✅ {r.summary()}")
                for m in r.messages[:3]:
                    lines.append(f"   · {m}")
                if up:
                    lines.append("")
                    lines.append(f"✅ {t('已上传为新版本')}（{up.get('version_time', '')}）")
                    lines.append(f"   {t('其他人重新下载即可；旧版本仍可回滚。')}")
            elif r.status == "is_mine":
                lines.append(f"✅ {t('内嵌数据属于你自己，云端版本无需改动。')}")
            elif r.status == "already_clean":
                lines.append(f"✅ {t('该云端存档已经是共享友好的，无需处理。')}")
            elif r.status == "running":
                lines.append(f"⏸ {t('本地有存档被游戏占用，已跳过。')}")
            else:
                lines.append(f"❌ {r.summary()}  {'；'.join(r.messages[:2])}")
            lines += ["", f"💡 {t('上传时已默认自动处理，此后新上传的存档无需再点这里。')}"]
            self._set_text("\n".join(lines))

        def fail(e):
            self._busy = False
            self._prog("")
            self._append(f"\n❌ {t('处理失败')}：{e}\n")

        self.app.run_async(work, done, fail)

    def refresh_theme(self):
        c = theme.colors()
        try:
            self.txt.configure(fg_color=c["surface"], text_color=c["text"])
            self.b_cloud.configure(fg_color=c["accent"], hover_color=c["accent_h"])
        except Exception:
            pass
