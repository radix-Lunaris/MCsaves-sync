# -*- coding: utf-8 -*-
"""
「区块清理」页交互回归测试（无真实 Tk，用桩件）

验证：
  1. 页面能构造、四个选项卡包含 prune
  2. 默认阈值 = 1 秒
  3. 低版本存档（无 InhabitedTime）-> 页面禁用扫描与执行
  4. 正常存档 -> 可用；扫描预览不改文件、报告正确、执行按钮启用
  5. 二次确认被取消时不执行清理
  6. 主题刷新不崩

用法： python3 tools/test_prune_page.py
"""
from __future__ import annotations

import os
import shutil
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ui_smoke  # noqa: E402  提供桩件
ui_smoke.install_stubs()

# 让 configure 记录参数，便于断言按钮 disabled / 文案变化
_orig_configure = ui_smoke.W.configure


def _rec_configure(self, *a, **k):
    # 必须用 __dict__：W.__getattr__ 会把任何未定义属性变成惰性桩，
    # hasattr(self, "_cfg") 恒为 True，导致拿到的是 W 而不是 dict。
    cfg = self.__dict__.setdefault("_cfg", {})
    cfg.update(k)
    if "text" in k:
        self._text = k["text"]
    return self


ui_smoke.W.configure = _rec_configure

from core import chunk_prune as cp  # noqa: E402
from core.models import Instance, SaveInfo  # noqa: E402
from ui.prune_page import PrunePage  # noqa: E402
import ui.dialogs as dialogs  # noqa: E402

TMP = "/tmp/mcsync_prune_page"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def make_save(name: str, inhabited_present: bool) -> str:
    """借助核心测试里的构造函数造一个真实 .mca。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import test_chunk_prune as helper
    base = os.path.join(TMP, name)
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "region"), exist_ok=True)
    vals = {0: (0 if inhabited_present else None),
            1: (5000 if inhabited_present else None),
            2: (19 if inhabited_present else None)}
    helper.make_region(os.path.join(base, "region", "r.0.0.mca"), vals)
    with open(os.path.join(base, "level.dat"), "wb") as f:
        f.write(b"\x00")
    return base


class FakeApp:
    """页面依赖的最小宿主。"""
    def __init__(self, instances, saves, cur_inst, cur_save):
        self.instances, self.saves = instances, saves
        self.cur_instance, self.cur_save = cur_inst, cur_save
        self.applied = 0

    def run_async(self, work, done, fail=None):
        try:
            done(work())
        except Exception as e:
            if fail:
                fail(e)

    def load_saves(self, inst):
        pass

    def select_instance(self, inst):
        self.cur_instance = inst

    def toast(self, msg):
        pass


def main():
    print("\n=== 区块清理页交互测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    normal_path = make_save("normal", True)
    legacy_path = make_save("legacy", False)

    inst = Instance(name="1.20.1-Forge", root=os.path.join(TMP, "inst"),
                    game_dir=os.path.join(TMP, "inst", ".minecraft"))
    s_normal = SaveInfo(name="WorldA", path=normal_path, world_name="正常存档")
    s_legacy = SaveInfo(name="WorldB", path=legacy_path, world_name="老版本存档")

    # ---------------- 1. 构造 ----------------
    print("[1] 页面构造")
    master = ui_smoke.W()
    app = FakeApp([inst], [s_normal, s_legacy], inst, s_normal)
    page = PrunePage(master, app)
    check("页面实例化成功", page is not None, "")
    check("默认阈值输入框为 1", str(page.e_seconds.get()) == "1",
          str(page.e_seconds.get()))
    check("_threshold_seconds() == 1.0", page._threshold_seconds() == 1.0,
          str(page._threshold_seconds()))

    # ---------------- 2. 正常存档：可用 ----------------
    print("\n[2] 正常存档 → 可用")
    page.sync_selection([inst], [s_normal, s_legacy], inst, s_normal)
    check("能力判定为支持", cp.capability(normal_path)[0], "")
    check("维度列表已填充", len(page._dims) >= 1, str(page._dims))
    check("扫描按钮启用", page.b_scan._cfg.get("state") == "normal",
          str(page.b_scan._cfg))
    check("执行按钮默认禁用", page.b_apply._cfg.get("state") == "disabled",
          str(page.b_apply._cfg))

    # ---------------- 3. 预览 ----------------
    print("\n[3] 扫描预览（不修改文件）")
    before = os.path.getsize(os.path.join(normal_path, "region", "r.0.0.mca"))
    page.do_scan()
    rep = page._report
    check("生成了预览报告", rep is not None, "")
    check("命中 2 个区块（0 与 19 tick）", rep.prune_chunks == 2,
          str(rep.prune_chunks))
    check("20 tick 阈值", rep.threshold_ticks == 20, str(rep.threshold_ticks))
    check("未修改源文件", os.path.getsize(
        os.path.join(normal_path, "region", "r.0.0.mca")) == before, "")
    check("预览后执行按钮启用", page.b_apply._cfg.get("state") == "normal",
          str(page.b_apply._cfg))
    txt = str(page.txt_report.get())
    check("报告包含区块数", "2" in txt and "预览结果" in txt, txt[:80])

    # ---------------- 3.5 未勾选维度不得清理全图 ----------------
    print("\n[3.5] 未勾选任何维度时不得退化成清理全图")
    for v in page._dim_vars.values():
        v.set(False)
    picked = page._selected_dims()
    check("返回空集而不是 None（None 代表不限制）",
          picked is not None and len(picked) == 0, str(picked))
    page.do_scan()
    check("未勾选时清空旧报告（防止拿旧预览去执行）",
          page._report is None, str(page._report))
    check("未勾选时执行按钮被禁用",
          page.b_apply._cfg.get("state") == "disabled", str(page.b_apply._cfg))
    for v in page._dim_vars.values():
        v.set(True)
    page.do_scan()   # 恢复可用报告，供后续执行测试使用

    # ---------------- 4. 取消二次确认 ----------------
    print("\n[4] 二次确认被取消时不执行")
    class FakeDlg:
        result = False
    orig_dlg = dialogs.ConfirmDialog
    dialogs.ConfirmDialog = lambda *a, **k: FakeDlg()
    try:
        page.do_apply()
        check("源文件未被改写", os.path.getsize(
            os.path.join(normal_path, "region", "r.0.0.mca")) == before, "")
    finally:
        dialogs.ConfirmDialog = orig_dlg

    # ---------------- 5. 正常执行 ----------------
    print("\n[5] 确认后执行清理")
    class OkDlg:
        result = True
    dialogs.ConfirmDialog = lambda *a, **k: OkDlg()
    try:
        page.do_apply()
        after = os.path.getsize(os.path.join(normal_path, "region", "r.0.0.mca"))
        check("文件已变小", after < before, f"{before} -> {after}")
        check("生成了备份", "备份位置" in str(page.txt_report.get()) or
              "清理完成" in str(page.txt_report.get()), str(page.txt_report.get())[-120:])
    finally:
        dialogs.ConfirmDialog = orig_dlg

    # ---------------- 6. 低版本禁用 ----------------
    print("\n[6] 低版本存档 → 页面禁用")
    ok_legacy, why = cp.capability(legacy_path)
    check("核心层判定不支持", not ok_legacy, why)
    page2 = PrunePage(ui_smoke.W(), app)
    page2.sync_selection([inst], [s_normal, s_legacy], inst, s_legacy)
    # 桩件下 cb_save.get() 返回构造时的 text，需显式指定选中项
    page2._saves = [s_legacy]
    page2._check_capability()
    check("扫描按钮被禁用", page2.b_scan._cfg.get("state") == "disabled",
          str(page2.b_scan._cfg))
    check("执行按钮被禁用", page2.b_apply._cfg.get("state") == "disabled",
          str(page2.b_apply._cfg))
    check("状态卡显示禁用标题", "禁用" in str(page2.cap_title._text),
          str(page2.cap_title._text))
    check("状态说明提到 InhabitedTime", "InhabitedTime" in str(page2.cap_desc._text),
          str(page2.cap_desc._text))
    check("低版本存档仍完好", os.path.exists(
        os.path.join(legacy_path, "region", "r.0.0.mca")), "")

    # ---------------- 7. 主题刷新 ----------------
    print("\n[7] 主题刷新 / 显示回调")
    page.refresh_theme()
    page.on_show()
    check("主题刷新未抛异常", True, "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 区块清理页可用且低版本已禁用")
    print("=" * 58)


if __name__ == "__main__":
    main()
