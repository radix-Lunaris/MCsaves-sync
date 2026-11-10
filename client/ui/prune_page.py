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
「区块清理」页。

交互原则：默认只预览，不动任何文件；只有用户看过报告、再点一次执行
并二次确认，才会真正改写存档。低版本存档（不写 InhabitedTime）直接禁用。
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Set

import customtkinter as ctk

from core import chunk_prune as cp
from core.i18n import t
from core.protocol import human_size
from ui import design as ds
from ui import theme


class PrunePage(ctk.CTkFrame):
    """区块清理页。由主窗口挂到 pages['prune']。"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        c = theme.colors()
        sp, fs, rd, sz = ds.SP, ds.FS, ds.RD, ds.SZ

        self.grid_rowconfigure(5, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._report: Optional[cp.PruneReport] = None
        self._dims: List[str] = []
        self._dim_vars: Dict[str, object] = {}
        self._scanning = False
        self._insts: List = []
        self._saves: List = []

        # ---------------- 标题与说明 ----------------
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=sp["s"], pady=(sp["m"], sp["xs"]))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=t("🧹 区块清理"), font=theme.font(fs["h1"], "bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head,
                     text=t("按玩家在区块内的累计停留时间筛选。默认只清理不足 1 秒的区块，"
                            "基本不会误删有内容的区域。切换上方「游戏实例」即可处理其他整合包；"
                            "主世界、下界、末地与自定义维度会自动全部识别。"),
                     font=theme.font(fs["sub"]), text_color=c["muted"],
                     anchor="w", justify="left", wraplength=760).grid(
            row=1, column=0, sticky="w", pady=(sp["xxs"], 0))

        # ---------------- 能力状态卡 ----------------
        self.cap_card = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["surface"],
                                     border_width=1, border_color=c["line"])
        self.cap_card.grid(row=1, column=0, sticky="ew", padx=sp["s"], pady=(sp["xs"], sp["xs"]))
        self.cap_card.grid_columnconfigure(1, weight=1)
        self.cap_icon = ctk.CTkLabel(self.cap_card, text="⏳", font=theme.font(fs["h2"]),
                                     width=36)
        self.cap_icon.grid(row=0, column=0, padx=(sp["m"], sp["xs"]), pady=sp["s"])
        self.cap_title = ctk.CTkLabel(self.cap_card, text=t("请先选择一个存档"),
                                      font=theme.font(fs["body"], "bold"), anchor="w")
        self.cap_title.grid(row=0, column=1, sticky="w", pady=(sp["s"], 0))
        self.cap_desc = ctk.CTkLabel(self.cap_card, text="", font=theme.font(fs["cap"]),
                                     text_color=c["muted"], anchor="w", justify="left",
                                     wraplength=720)
        self.cap_desc.grid(row=1, column=1, sticky="w", pady=(0, sp["s"]))

        # ---------------- 参数区 ----------------
        opt = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["card2"],
                           border_width=1, border_color=c["line"])
        opt.grid(row=2, column=0, sticky="ew", padx=sp["s"], pady=sp["xs"])
        opt.grid_columnconfigure(1, weight=1)
        opt.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(opt, text=t("游戏实例"), font=theme.font(fs["sub"])).grid(
            row=0, column=0, sticky="w", padx=(sp["m"], sp["xs"]), pady=(sp["s"], sp["xxs"]))
        self.cb_inst = ctk.CTkComboBox(opt, values=[], font=theme.font(fs["sub"]),
                                       height=sz["entry_h"], command=self._on_inst_changed)
        self.cb_inst.grid(row=0, column=1, sticky="ew", padx=(0, sp["m"]), pady=(sp["s"], sp["xxs"]))

        ctk.CTkLabel(opt, text=t("存档"), font=theme.font(fs["sub"])).grid(
            row=0, column=2, sticky="w", padx=(sp["xs"], sp["xs"]), pady=(sp["s"], sp["xxs"]))
        self.cb_save = ctk.CTkComboBox(opt, values=[], font=theme.font(fs["sub"]),
                                       height=sz["entry_h"], command=self._on_save_changed)
        self.cb_save.grid(row=0, column=3, sticky="ew", padx=(0, sp["m"]), pady=(sp["s"], sp["xxs"]))

        ctk.CTkLabel(opt, text=t("停留时间小于"), font=theme.font(fs["sub"])).grid(
            row=1, column=0, sticky="w", padx=(sp["m"], sp["xs"]), pady=(sp["xs"], sp["s"]))
        thr = ctk.CTkFrame(opt, fg_color="transparent")
        thr.grid(row=1, column=1, sticky="w", pady=(sp["xs"], sp["s"]))
        self.e_seconds = ctk.CTkEntry(thr, width=72, height=sz["entry_h"],
                                      font=theme.font(fs["sub"]))
        self.e_seconds.pack(side="left")
        self.e_seconds.insert(0, str(cp.DEFAULT_MAX_SECONDS))
        ctk.CTkLabel(thr, text=t("秒（20 tick/秒）· 默认 1 秒"),
                     font=theme.font(fs["cap"]), text_color=c["muted"]).pack(
            side="left", padx=(sp["xs"], 0))

        ctk.CTkLabel(opt, text=t("清理维度"), font=theme.font(fs["sub"])).grid(
            row=1, column=2, sticky="w", padx=(sp["xs"], sp["xs"]), pady=(sp["xs"], sp["s"]))
        self.dim_box = ctk.CTkFrame(opt, fg_color="transparent")
        self.dim_box.grid(row=1, column=3, sticky="w", pady=(sp["xs"], sp["s"]))

        # ---------------- 按钮区 ----------------
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.grid(row=3, column=0, sticky="ew", padx=sp["s"], pady=sp["xs"])
        act.grid_columnconfigure(4, weight=1)
        self.b_scan = ctk.CTkButton(act, text=t("🔍 扫描预览（不修改文件）"),
                                    height=sz["btn_h"], font=theme.font(fs["body"], "bold"),
                                    command=self.do_scan)
        self.b_scan.grid(row=0, column=0, padx=(0, sp["xs"]))
        self.b_apply = ctk.CTkButton(act, text=t("🧹 执行清理"),
                                     height=sz["btn_h"], font=theme.font(fs["body"], "bold"),
                                     fg_color=c["danger"], hover_color="#a33933",
                                     state="disabled", command=self.do_apply)
        self.b_apply.grid(row=0, column=1, padx=(0, sp["xs"]))
        self.l_progress = ctk.CTkLabel(act, text="", font=theme.font(fs["cap"]),
                                       text_color=c["muted"], anchor="w")
        self.l_progress.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(act, text=t("先预览，确认后再执行"),
                     font=theme.font(fs["cap"]), text_color=c["muted"]).grid(
            row=0, column=5, sticky="e")

        # ---------------- 报告区 ----------------
        rep_frame = ctk.CTkFrame(self, corner_radius=rd["card"], fg_color=c["card2"],
                                 border_width=1, border_color=c["line"])
        rep_frame.grid(row=5, column=0, sticky="nsew", padx=sp["s"], pady=(sp["xs"], sp["xs"]))
        rep_frame.grid_rowconfigure(1, weight=1)
        rep_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(rep_frame, text=t("预览报告"), font=theme.font(fs["h2"], "bold")).grid(
            row=0, column=0, sticky="w", padx=sp["m"], pady=(sp["s"], sp["xxs"]))
        self.txt_report = ctk.CTkTextbox(rep_frame, font=theme.font(fs["sub"]),
                                         fg_color=c["surface"], text_color=c["text"],
                                         corner_radius=rd["ctl"], wrap="word")
        self.txt_report.grid(row=1, column=0, sticky="nsew",
                             padx=sp["m"], pady=(0, sp["m"]))
        self._set_report(t("尚未扫描。选择存档后点击「扫描预览」，这里会列出将被删除的区块数量、"
                           "涉及的维度与文件、预计释放的空间，以及被跳过（无法判定）的区块。"))

        # ---------------- 风险提示 ----------------
        warn = ctk.CTkLabel(
            self,
            text=t("⚠ 删除后地形会按种子重新生成，操作不可撤销。执行前会自动备份完整存档；"
                   "云端历史版本仍保留，必要时可从版本管理回滚。请关闭游戏后再执行。"),
            font=theme.font(fs["cap"]), text_color=c["warn"],
            anchor="w", justify="left", wraplength=900)
        warn.grid(row=6, column=0, sticky="ew", padx=sp["s"], pady=(0, sp["m"]))

    # ------------------------------------------------------------ 工具
    def _set_report(self, text: str):
        try:
            self.txt_report.delete("1.0", "end")
            self.txt_report.insert("1.0", text)
        except Exception:
            pass

    def _append_report(self, text: str):
        try:
            self.txt_report.insert("end", text)
        except Exception:
            pass

    def _progress(self, text: str):
        try:
            self.l_progress.configure(text=text)
        except Exception:
            pass

    def _set_cap(self, icon: str, title: str, desc: str, color: str = ""):
        c = theme.colors()
        try:
            self.cap_icon.configure(text=icon, text_color=color or c["text"])
            self.cap_title.configure(text=title)
            self.cap_desc.configure(text=desc)
        except Exception:
            pass

    def _disable_all(self, reason: str):
        self._report = None
        try:
            self.b_scan.configure(state="disabled")
            self.b_apply.configure(state="disabled")
        except Exception:
            pass
        self._set_cap("⛔", t("已禁用区块清理"), reason, theme.colors()["danger"])

    # ------------------------------------------------------------ 数据同步
    def sync_selection(self, instances, saves, cur_inst, cur_save):
        """主窗口切到本页 / 实例或存档变化时调用。"""
        self._insts = list(instances or [])
        self._provided = list(saves or [])
        names = [i.name for i in self._insts]
        try:
            self.cb_inst.configure(values=names)
            if cur_inst and cur_inst.name in names:
                self.cb_inst.set(cur_inst.name)
            elif names:
                self.cb_inst.set(names[0])
        except Exception:
            pass
        self._refresh_saves(cur_inst, cur_save)

    def _saves_for(self, inst, provided=None):
        """按实例独立列出存档。

        旧版直接用主窗口传进来的 app.saves —— 那是「当前选中实例」的存档，
        于是无论在这页怎么切实例，下拉里永远只有最初那个整合包的存档，
        表现就是「多个整合包，只有一个能被清理」。
        MCA Selector 是「找到 region 文件夹就当作一个可处理世界」，
        这里同样不对整合包做白名单，每个实例各自扫描自己的 saves 目录。
        """
        if inst is None:
            return []
        base = os.path.normcase(os.path.abspath(inst.saves_dir or ""))
        cand = []
        for s in (provided or []):
            try:
                if os.path.normcase(os.path.abspath(s.path)).startswith(base + os.sep):
                    cand.append(s)
            except Exception:
                pass
        if cand:
            return cand
        try:
            from core.scanner import InstanceScanner
            scanned = list(InstanceScanner.list_saves(inst, quick=True) or [])
        except Exception:
            scanned = []
        if scanned:
            return scanned
        # 该实例目录扫不到存档时，若只有一个实例，允许沿用主窗口带进来的
        # 存档对象（例如手动指定的绿色版路径）。多实例场景绝不能混用，
        # 否则切到别的整合包还会显示上一个整合包的存档。
        if len(getattr(self, "_insts", []) or []) <= 1:
            return list(provided or [])
        return []

    def _refresh_saves(self, cur_inst, cur_save):
        inst = self._pick_inst(cur_inst)
        if inst is None:
            try:
                self.cb_save.configure(values=[])
            except Exception:
                pass
            self._dims = []
            self._rebuild_dims()
            self._disable_all(t("请先选择一个游戏实例和存档"))
            return
        provided = None
        if cur_inst and inst.key == cur_inst.key:
            provided = getattr(self, "_provided", None)
        self._saves = self._saves_for(inst, provided)
        save_names = [s.display_name for s in (self._saves or [])]
        try:
            self.cb_save.configure(values=save_names)
            if cur_save and cur_save.display_name in save_names:
                self.cb_save.set(cur_save.display_name)
            elif save_names:
                self.cb_save.set(save_names[0])
        except Exception:
            pass
        self._check_capability()

    def _pick_inst(self, cur_inst):
        for i in (self._insts or []):
            if cur_inst and i.key == cur_inst.key:
                return i
        return (self._insts or [None])[0]

    def _pick_save(self, cur_save):
        for s in (self._saves or []):
            if cur_save and s.path == cur_save.path:
                return s
        return (self._saves or [None])[0]

    def _on_inst_changed(self, _choice=None):
        # 只刷新本页自己的存档下拉，不改主窗口的当前实例：
        # 之前调 app.select_instance 会把主窗口一起切走，且本页 _saves 仍不更新，
        # 结果就是切了实例却还是原来那批存档。
        inst = self._selected_inst()
        self._refresh_saves(inst, None)

    def _on_save_changed(self, _choice=None):
        self._check_capability()

    def _selected_inst(self):
        name = ""
        try:
            name = str(self.cb_inst.get() or "")
        except Exception:
            pass
        for i in (self._insts or []):
            if i.name == name:
                return i
        return (self._insts or [None])[0]

    def _selected_save(self):
        name = ""
        try:
            name = str(self.cb_save.get() or "")
        except Exception:
            pass
        for s in (self._saves or []):
            if s.display_name == name or s.name == name:
                return s
        return (self._saves or [None])[0]

    def _threshold_seconds(self) -> float:
        try:
            v = float(str(self.e_seconds.get() or "1").strip())
            return max(0.0, min(3600.0, v))
        except (TypeError, ValueError):
            return float(cp.DEFAULT_MAX_SECONDS)

    # ------------------------------------------------------------ 能力检测
    def _check_capability(self):
        save = self._selected_save()
        if save is None or not os.path.isdir(save.path):
            self._disable_all(t("未找到可清理的存档目录"))
            return
        try:
            ok, why = cp.capability(save.path)
        except Exception as e:
            self._disable_all(f"{t('检测失败')}：{e}")
            return
        if not ok:
            self._disable_all(why)
            return
        c = theme.colors()
        desc = t("该存档记录玩家停留时间，可按阈值安全筛选。") + (f"  {why}" if why else "")
        self._set_cap("✅", t("可用"), desc, c["ok"])
        try:
            self.b_scan.configure(state="normal")
            self.b_apply.configure(state="disabled")
        except Exception:
            pass
        self._dims = [d["label"] for d in cp.discover_dimensions(save.path)]
        self._rebuild_dims()
        self._report = None

    def _rebuild_dims(self):
        for child in list(getattr(self.dim_box, "winfo_children", lambda: [])()):
            try:
                child.destroy()
            except Exception:
                pass
        self._dim_vars = {}
        c = theme.colors()
        if not self._dims:
            ctk.CTkLabel(self.dim_box, text=t("—"), font=theme.font(ds.FS["cap"]),
                         text_color=c["muted"]).pack(side="left")
            return
        for i, label in enumerate(self._dims):
            v = ctk.BooleanVar(value=True)
            self._dim_vars[label] = v
            ctk.CTkCheckBox(self.dim_box, text=label, variable=v,
                            font=theme.font(ds.FS["cap"]),
                            fg_color=c["accent"], border_color=c["line_hi"],
                            text_color=c["text2"]).pack(side="left", padx=(0, ds.SP["s"]))

    def _selected_dims(self) -> Optional[Set[str]]:
        if not self._dims:
            return None
        picked = set()
        for label, v in self._dim_vars.items():
            try:
                if bool(v.get()):
                    picked.add(label)
            except Exception:
                pass
        # 必须是空集而不是 None：None 在 scan() 里表示"不限制、全部清理"，
        # 用户一个维度都没勾却清了全图，那是最危险的误操作。
        return picked

    # ------------------------------------------------------------ 扫描
    def do_scan(self):
        if self._scanning:
            return
        save = self._selected_save()
        if save is None:
            return
        seconds = self._threshold_seconds()
        dims = self._selected_dims()
        if dims is not None and not dims:
            # 必须清掉上一次的预览报告并禁用执行按钮：
            # 否则用户改了勾选范围后，仍可能拿着旧报告去点「执行清理」。
            self._report = None
            try:
                self.b_apply.configure(state="disabled")
            except Exception:
                pass
            self._set_report(t("请至少勾选一个要清理的维度。"))
            return
        self._scanning = True
        self._progress(t("正在扫描…"))
        self._set_report(t("正在解析 region / entities / poi，请稍候…\n"))
        try:
            self.b_scan.configure(state="disabled")
            self.b_apply.configure(state="disabled")
        except Exception:
            pass

        def work():
            return cp.scan(save.path, seconds, dims)

        def done(rep):
            self._scanning = False
            self._render_report(rep, seconds)
            try:
                self.b_scan.configure(state="normal")
                self.b_apply.configure(state="normal" if rep.safe_to_apply else "disabled")
            except Exception:
                pass

        def fail(e):
            self._scanning = False
            self._progress("")
            self._set_report(f"{t('扫描失败')}：{e}")
            try:
                self.b_scan.configure(state="normal")
            except Exception:
                pass

        self.app.run_async(work, done, fail)

    def _render_report(self, rep: cp.PruneReport, seconds: float):
        self._report = rep
        self._progress("")
        if not rep.supported:
            self._set_report(f"⛔ {rep.unsupported_reason}")
            return
        if rep.prune_chunks <= 0:
            self._set_report(
                f"✅ {t('没有符合条件的区块')}\n\n"
                f"{t('阈值')}：{seconds:g} {t('秒')}（{rep.threshold_ticks} tick）\n"
                f"{t('扫描')} {rep.total_chunks} {t('个区块')}，"
                f"{rep.skipped_chunks} {t('个因无法判定被保留')}。\n\n"
                f"{t('当前阈值下没有需要清理的区块，存档已经是干净的。')}")
            return

        lines = [
            f"🔍 {t('预览结果')}（{t('未修改任何文件')}）",
            "",
            f"  {t('筛选条件')}：InhabitedTime < {rep.threshold_ticks} tick"
            f"（{seconds:g} {t('秒')}）",
            f"  {t('将删除')}：{rep.prune_chunks} {t('个区块')}"
            f" / {t('共扫描')} {rep.total_chunks} {t('个')}",
            f"  {t('涉及文件')}：{rep.affected_files} {t('个')}"
            f"（{t('其中整个文件被删')} {rep.delete_files} {t('个')}）",
            f"  {t('预计释放')}：{human_size(rep.freed_bytes)}",
            f"  {t('无法判定而保留')}：{rep.skipped_chunks} {t('个区块')}"
            f"（{t('缺字段 / 压缩或 NBT 无法解析')}）",
            "",
            f"📊 {t('按维度')}：",
        ]
        for label, n in sorted(rep.dimensions.items(), key=lambda kv: -kv[1]):
            lines.append(f"    · {label}：{n} {t('个区块')}")
        lines += ["", f"📁 {t('涉及文件明细')}："]
        for r in rep.regions:
            if r.prune <= 0 and not r.will_delete_file:
                continue
            tag = f"[{r.kind}]"
            flag = t("（清空后删除文件）") if r.will_delete_file else ""
            lines.append(f"    · {r.dimension} {tag} {os.path.basename(r.path)}"
                         f"  -{r.prune} {t('区块')}{flag}")
        lines += [
            "",
            f"💡 {t('确认无误后再点「执行清理」。执行前会自动备份完整存档，')}"
            f"{t('并再次弹窗确认；游戏运行中会被拦截。')}",
        ]
        self._set_report("\n".join(lines))

    # ------------------------------------------------------------ 执行
    def do_apply(self):
        rep = self._report
        if rep is None or not rep.safe_to_apply:
            return
        save = self._selected_save()
        if save is None:
            return
        # 运行中拦截
        try:
            from core.sync import SyncEngine
            if SyncEngine.is_running(save.path):
                self._set_cap("⛔", t("游戏可能正在运行"),
                              t("请先关闭游戏再执行区块清理，避免读到正在写入的区块。"),
                              theme.colors()["warn"])
                return
        except Exception:
            pass

        from ui.dialogs import ConfirmDialog
        dlg = ConfirmDialog(
            self.app, t("二次确认 · 区块清理"),
            f"{t('即将从')}「{save.display_name}」{t('删除')} {rep.prune_chunks} "
            f"{t('个区块')}（{t('停留不足')} {rep.threshold_seconds:g} {t('秒')}）。\n\n"
            f"{t('· 删除后地形会按种子重新生成，此操作不可撤销')}\n"
            f"{t('· 执行前会自动完整备份存档')}\n"
            f"{t('· region / entities / poi 三目录会协同清理')}\n"
            f"{t('· 云端历史版本仍在，可从版本管理回滚')}\n\n"
            f"{t('确认继续吗？')}",
            ok_text=t("确认清理"), danger=True)
        if not getattr(dlg, "result", None):
            return

        self._progress(t("正在备份并清理…"))
        self._append_report(f"\n\n⏳ {t('正在执行…')}")
        try:
            self.b_scan.configure(state="disabled")
            self.b_apply.configure(state="disabled")
        except Exception:
            pass

        def work():
            return cp.apply(rep)

        def done(r):
            self._progress("")
            if r["ok"]:
                msg = (f"\n\n✅ {t('清理完成')}\n"
                       f"  {t('释放空间')}：{human_size(r['freed_bytes'])}\n"
                       f"  {t('处理文件')}：{r['touched_files']} "
                       f"{t('个')}（{t('删除空文件')} {r['deleted_files']} {t('个')}）\n"
                       f"  {t('备份位置')}：{r['backup']}\n\n"
                       f"💡 {t('记得把清理后的存档上传到云端，其他人才会拿到新版本。')}")
            else:
                msg = (f"\n\n⚠ {t('部分文件处理失败')}：\n"
                       + "\n".join(f"    · {e}" for e in r["errors"])
                       + f"\n{t('备份仍在')}：{r['backup']}")
            self._append_report(msg)
            try:
                self.b_scan.configure(state="normal")
                self.app.load_saves(self.app.cur_instance)
                self._check_capability()
            except Exception:
                pass

        def fail(e):
            self._progress("")
            self._append_report(f"\n\n❌ {t('清理失败')}：{e}")
            try:
                self.b_scan.configure(state="normal")
            except Exception:
                pass

        self.app.run_async(work, done, fail)

    # ------------------------------------------------------------ 主题 / 显示
    def refresh_theme(self):
        c = theme.colors()
        try:
            self.cap_card.configure(fg_color=c["surface"], border_color=c["line"])
            self.cap_desc.configure(text_color=c["muted"])
            self.b_apply.configure(fg_color=c["danger"])
            self.txt_report.configure(fg_color=c["surface"], text_color=c["text"])
        except Exception:
            pass
        # 侧栏选中态由主窗口统一维护，这里不重复处理

    def on_show(self):
        try:
            self.sync_selection(self.app.instances, self.app.saves,
                                self.app.cur_instance, self.app.cur_save)
        except Exception:
            pass
