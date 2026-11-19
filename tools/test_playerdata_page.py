# -*- coding: utf-8 -*-
"""
「玩家数据」页回归测试（无真实 Tk，用桩件）

验证：
  1. 页面精简：没有手动「扫描/修复全部」按钮，只留必要控件
  2. 云端存档列表加载
  3. 取消二次确认不产生任何网络/文件操作
  4. 确认后走「下载(跳过自动修正) → 修正 → 上传新版本」，且带上 instance
  5. 已是共享友好 / 属于自己 时不生成新版本
  6. 开关状态标签、主题刷新、主窗口页签

用法： python3 tools/test_playerdata_page.py
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()


def _rec_configure(self, *a, **k):
    cfg = self.__dict__.setdefault("_cfg", {})
    cfg.update(k)
    if "text" in k:
        self._text = k["text"]
    return self


ui_smoke.W.configure = _rec_configure

from core import nbt_typed as N            # noqa: E402
from core import playerdata as pd          # noqa: E402
from core.config import cfg                # noqa: E402
from ui.playerdata_page import PlayerDataPage  # noqa: E402
import ui.dialogs as dialogs               # noqa: E402
import core.sync as sync_mod               # noqa: E402
import test_playerdata as h                # noqa: E402

TMP = "/tmp/mcsync-pd-page"
OK: list = []
CALLS: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


class FakeApi:
    def __init__(self):
        self.saves = [{"name": "MyWorld", "instance": "All the Mods", "kind": "save",
                       "display": "MyWorld"},
                      {"name": "Config-Pack", "instance": "All the Mods", "kind": "config"}]

    def list_saves(self):
        CALLS.append(("list_saves",))
        return list(self.saves)


class FakeApp:
    def __init__(self):
        self.api = FakeApi()
        self.cur_instance = None
        self.cur_save = None
        self.instances = []

    def run_async(self, work, done, fail=None):
        try:
            done(work())
        except Exception as e:
            if fail:
                fail(e)

    def toast(self, msg):
        pass


class FakeEngine:
    """替身：记录调用顺序，并在 download 时造一份带房主数据的存档。"""

    def __init__(self, api, log=None):
        self.api = api
        self._level_kind = "legacy"

    def download(self, save_dir, name, auto_fix_playerdata=None):
        CALLS.append(("download", auto_fix_playerdata))
        os.makedirs(save_dir, exist_ok=True)
        if self._level_kind == "legacy":
            h.make_level(os.path.join(save_dir, "level.dat"), "legacy", h.OWNER)
        else:
            h.make_level(os.path.join(save_dir, "level.dat"), "clean", h.OWNER)
        return {"ok": True}

    def upload(self, save_dir, name, instance="", kind="save"):
        CALLS.append(("upload", instance, kind))
        return {"ok": True, "version_time": "2026-11-10 22:00"}


def main():
    print("\n=== 玩家数据页（精简 + 云端补修）测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    old_dir, old_data = cfg.dir, dict(cfg.data)
    cfg.dir = os.path.join(TMP, "cfg")
    os.makedirs(cfg.dir, exist_ok=True)
    cfg.set("my_uuid", "")
    cfg.set("auto_fix_playerdata", True)

    orig_engine = sync_mod.SyncEngine
    sync_mod.SyncEngine = FakeEngine
    orig_dlg = dialogs.ConfirmDialog
    try:
        # ---------------- 1. 页面精简 ----------------
        print("[1] 页面只留必要内容")
        app = FakeApp()
        page = PlayerDataPage(ui_smoke.W(), app)
        check("实例化成功", page is not None, "")
        # W 桩件的 __getattr__ 会为任意属性返回惰性桩，hasattr 恒为 True，
        # 必须直接查实例字典。
        check("没有手动「修复全部」按钮", "b_fix" not in page.__dict__, "")
        check("没有手动「扫描全部」按钮", "b_scan" not in page.__dict__, "")
        check("有云端存档下拉", "cb_cloud" in page.__dict__, "")
        check("有云端处理按钮", "b_cloud" in page.__dict__, "")
        check("自动处理标签显示上传前开启",
              "上传前" in str(page.l_auto._text), str(page.l_auto._text))
        check("默认 UUID 为空", str(page.e_uuid.get()) == "", str(page.e_uuid.get()))

        # ---------------- 2. 云端列表 ----------------
        print("\n[2] 云端存档列表")
        page.on_show()
        values = page.cb_cloud._cfg.get("values", [])
        check("只列出 kind=save 的存档",
              values == ["MyWorld"], str(values))
        check("调用过 list_saves", ("list_saves",) in CALLS, str(CALLS))

        # ---------------- 3. 取消确认 ----------------
        print("\n[3] 取消二次确认不动任何东西")
        class NoDlg:
            result = False
        dialogs.ConfirmDialog = lambda *a, **k: NoDlg()
        CALLS.clear()
        page.cb_cloud._text = "MyWorld"
        page.do_cloud_fix()
        check("没有 download / upload",
              not any(c[0] in ("download", "upload") for c in CALLS), str(CALLS))

        # ---------------- 4. 确认后云端补修 ----------------
        print("\n[4] 确认后：下载 → 修正 → 上传新版本")
        class YesDlg:
            result = True
        dialogs.ConfirmDialog = lambda *a, **k: YesDlg()
        CALLS.clear()
        page.do_cloud_fix()
        check("download 时显式关闭自动修正（避免双重处理）",
              ("download", False) in CALLS, str(CALLS))
        check("上传生成了新版本",
              any(c[0] == "upload" for c in CALLS), str(CALLS))
        up = [c for c in CALLS if c[0] == "upload"][0]
        check("上传保留原整合包归属", up[1] == "All the Mods", str(up))
        check("上传 kind 为 save", up[2] == "save", str(up))
        txt = str(page.txt.get())
        check("报告说明已上传新版本", "新版本" in txt, txt[:160])
        check("报告提醒可回滚", "回滚" in txt, txt[:200])

        # ---------------- 5. 已是共享友好 ----------------
        print("\n[5] 云端已干净时不生成新版本")
        clean_engine = FakeEngine(app.api)
        clean_engine._level_kind = "clean"
        sync_mod.SyncEngine = lambda api, log=None: clean_engine
        CALLS.clear()
        page.do_cloud_fix()
        check("没有上传（无需改动）",
              not any(c[0] == "upload" for c in CALLS), str(CALLS))
        check("报告说明无需处理",
              "无需处理" in str(page.txt.get()), str(page.txt.get())[:140])

        # ---------------- 6. 属于自己 ----------------
        print("\n[6] 填了 my_uuid 且属于自己时不动")
        sync_mod.SyncEngine = FakeEngine
        cfg.set("my_uuid", str(h.OWNER))
        page.e_uuid._text = str(h.OWNER)
        CALLS.clear()
        page.do_cloud_fix()
        check("没有上传",
              not any(c[0] == "upload" for c in CALLS), str(CALLS))
        check("报告说明属于自己",
              "属于你自己" in str(page.txt.get()), str(page.txt.get())[:140])
        cfg.set("my_uuid", "")
        page.e_uuid._text = ""

        # ---------------- 7. 开关与主题 ----------------
        print("\n[7] 开关标签与主题刷新")
        cfg.set("auto_fix_playerdata", False)
        page._refresh_auto_label()
        check("关闭时标签正确", "已关闭" in str(page.l_auto._text), str(page.l_auto._text))
        cfg.set("auto_fix_playerdata", True)
        page.refresh_theme()
        check("主题刷新不抛异常", True, "")

        # ---------------- 8. 主窗口页签 ----------------
        print("\n[8] 主窗口页签完整")
        import ui.main_window as mw
        a = mw.App()
        keys = [x[0] for x in a.tabs]
        check("含 playerdata 页", "playerdata" in keys, str(keys))
        check("页面已创建", hasattr(a, "playerdata_page"), "")
        a.show_page("playerdata")
        check("可切换到玩家数据页", a.cur_page == "playerdata", a.cur_page)

    finally:
        sync_mod.SyncEngine = orig_engine
        dialogs.ConfirmDialog = orig_dlg
        cfg.dir = old_dir
        cfg.data = old_data

    print("\n" + "=" * 60)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 精简页与云端补修可用")
    print("=" * 60)


if __name__ == "__main__":
    main()
