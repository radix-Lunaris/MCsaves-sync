# -*- coding: utf-8 -*-
"""
本轮两项需求 + 自检发现的隐藏 bug 的回归测试。

需求 1：本地存档卡片加「🪪 UUID 修复」按钮，点击直接清掉本地 level.dat
        里内嵌的房主 UUID（不上传云端）；上传时自动处理的功能保持不变。
需求 2：各页面显示不全 —— 重点是「云端存档 → 版本 → 下载」那个弹窗，
        在朋友的高 DPI / 小屏设备上底部按钮被裁掉。

自检顺带发现：
  · SettingsDialog._build_sync 里定时同步的三个控件曾整段被误粘到
    InputDialog._ok 后面成了永不执行的死代码，而 _save() 无条件读它们，
    导致「设置 → 保存」直接 AttributeError 崩掉。
  · main.py 紧急设置窗口写死 520x300 + 不可调整。
  · 页面里 wraplength=860/900 等固定像素，窄窗口/DPI 缩放会把文字画到容器外。

用法： python3 tools/test_uuid_button_and_dialogs.py
"""
from __future__ import annotations

import inspect
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TMP = "/tmp/mcsync-uuid-dialogs"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MCSYNC_CONFIG_DIR"] = TMP

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def main():
    print("\n=== UUID 修复按钮 / 全局显示适配 回归 ===\n")

    # ================================================================ 1
    print("[1] 本地存档卡片出现「UUID 修复」按钮且不干扰卡片交互")
    from core.models import SaveInfo
    from ui.widgets import SaveCard, Card

    sv = SaveInfo(name="MyWorld", path=os.path.join(TMP, "MyWorld"))
    clicked = []
    selected = []
    doubled = []
    card = SaveCard(ui_smoke.W(), sv,
                    on_click=lambda: selected.append(1),
                    on_double=lambda: doubled.append(1),
                    on_fix=lambda s: clicked.append(s.display_name))

    btns = [w for w in _walk(card)
            if "UUID" in str(getattr(w, "_text", ""))]
    check("卡片里确实有 UUID 按钮", len(btns) == 1,
          str([getattr(w, "_text", "") for w in btns]))
    btn = btns[0]
    check("按钮标记为不冒泡到卡片",
          getattr(btn, "_mcsync_no_card_click", False) is True)
    # 桩件的 bind 不记录，改用源码断言 bind_click 会跳过标记
    src_widgets = src("client/ui/widgets.py")
    check("bind_click 会跳过标记的控件",
          "_mcsync_no_card_click" in inspect.getsource(Card.bind_click))
    check("按钮 command 绑定的是 on_fix",
          "command=lambda: self.on_fix(self.save)" in src_widgets)
    # 点击按钮：只应触发修复，不应触发选中/双击上传
    btn.kwargs["command"]()
    check("点击按钮触发 on_fix", clicked and clicked[0] == "MyWorld", str(clicked))
    check("不会顺带触发双击上传", not doubled, str(doubled))

    card2 = SaveCard(ui_smoke.W(), sv, on_click=lambda: selected.append(1))
    check("不传 on_fix 时不出现按钮",
          not any("UUID" in str(getattr(w, "_text", "")) for w in _walk(card2)))

    # ================================================================ 2
    print("\n[2] 主窗口入口：render_saves 传回调，fix_local_playerdata 真的执行")
    import ui.main_window as mw
    src_mw = src("client/ui/main_window.py")
    check("render_saves 传了 on_fix",
          "on_fix=lambda x=s: self.fix_local_playerdata(x)" in src_mw)
    check("上传自动处理仍保留",
          'cfg.get("auto_fix_playerdata", True)' in
          inspect.getsource(__import__("core.sync", fromlist=["SyncEngine"]).SyncEngine.upload))
    src_fix = inspect.getsource(mw.App.fix_local_playerdata)
    check("本地修复复用 core.playerdata.fix", "pd.fix(" in src_fix)
    check("本地修复不联网/不上传",
          "self.api" not in src_fix and "upload(" not in src_fix)
    check("游戏运行中拒绝", "st.running" in src_fix and "请先关闭游戏" in src_fix)
    check("已是共享友好时跳过", "st.needs_fix" in src_fix)
    check("二次确认", "ConfirmDialog" in src_fix and "dlg.result" in src_fix)
    check("完成后刷新列表", "self.load_saves(" in src_fix)
    check("失败不影响存档（有 fail 回调）",
          "self.run_async(work, done, fail)" in src_fix)

    # 行为测试：模拟需要修复 + 确认 → 走完 pd.fix 并刷新
    import core.playerdata as pd
    from types import SimpleNamespace
    real_inspect, real_fix = pd.inspect, pd.fix
    calls = {"fix": 0, "load": 0, "toast": [], "log": []}

    class FakePD:
        @staticmethod
        def inspect(p):
            return SimpleNamespace(running=False, needs_fix=True,
                                   summary=lambda: "⚠ 内嵌房主玩家数据",
                                   format="legacy")
        @staticmethod
        def fix(p, my_uuid=""):
            calls["fix"] += 1
            return SimpleNamespace(changed=True, status="fixed",
                                   summary=lambda: "✅ 已解除共享",
                                   messages=["归档完成"])

    pd.inspect, pd.fix = FakePD.inspect, FakePD.fix
    import ui.dialogs as dlgmod
    real_confirm = dlgmod.ConfirmDialog

    class FakeConfirm:
        def __init__(self, *a, **k):
            self.result = True

    dlgmod.ConfirmDialog = FakeConfirm
    mw.ConfirmDialog = FakeConfirm

    class StubApp:
        fix_local_playerdata = mw.App.fix_local_playerdata
        cur_instance = None

        def toast(self, m): calls["toast"].append(m)
        def log(self, m): calls["log"].append(m)
        def load_saves(self, inst, quick=False): calls["load"] += 1
        def run_async(self, work, done, fail=None):
            try:
                done(work())
            except Exception as e:
                if fail:
                    fail(e)

    app = StubApp()
    os.makedirs(sv.path, exist_ok=True)
    app.fix_local_playerdata(sv)
    check("确实调用了 pd.fix", calls["fix"] == 1, str(calls))
    check("修复后刷新存档列表", calls["load"] == 1)
    check("给出了成功提示", any("已解除共享" in m for m in calls["toast"]),
          str(calls["toast"]))

    # 取消确认时不应改动任何文件
    calls["fix"] = 0
    class FakeCancel(FakeConfirm):
        def __init__(self, *a, **k): self.result = None
    mw.ConfirmDialog = FakeCancel
    app.fix_local_playerdata(sv)
    check("取消确认不执行修复", calls["fix"] == 0, str(calls))

    # 游戏运行中直接拒绝
    calls["fix"] = 0
    FakePD.inspect = staticmethod(lambda p: SimpleNamespace(
        running=True, needs_fix=True, summary=lambda: "running"))
    pd.inspect = FakePD.inspect
    mw.ConfirmDialog = FakeConfirm
    app.fix_local_playerdata(sv)
    check("游戏运行中拒绝且不弹确认", calls["fix"] == 0, str(calls))

    pd.inspect, pd.fix = real_inspect, real_fix
    dlgmod.ConfirmDialog = real_confirm
    mw.ConfirmDialog = real_confirm

    # ================================================================ 3
    print("\n[3] 对话框公共底座：滚动内容 / 常驻底栏 / 按内容自适应 / 动态换行")
    from ui.dialogs import _BaseDialog
    src_base = inspect.getsource(_BaseDialog)
    check("默认创建可滚动 body", "CTkScrollableFrame" in src_base and "self.body" in src_base)
    check("footer 常驻窗口底部", "side=\"bottom\"" in src_base and "make_footer" in src_base)
    check("按 winfo_reqheight 自适应高度", "winfo_reqheight" in src_base)
    check("wraplength 跟随窗口宽度", "_apply_wrap" in src_base and "auto_wrap" in src_base)
    check("minsize 不再按比例卡死",
          "self.minsize(min(360, w)" in src_base and "int(w * 0.7)" not in src_base)
    check("屏幕可用高度留了标题栏+任务栏", "sh - 120" in src_base)

    # 真实构造四个短弹窗，验证内容挂在 body、按钮栏挂在 self
    d_src = src("client/ui/dialogs.py")
    check("_DownloadTargetDialog 内容改用 self.body",
          "ctk.CTkLabel(self.body" in d_src[d_src.index("class _DownloadTargetDialog"):
                                            d_src.index("class InputDialog")])
    check("_DownloadTargetDialog 底栏用 make_footer",
          "bar = self.make_footer(20)" in d_src[d_src.index("class _DownloadTargetDialog"):
                                               d_src.index("class InputDialog")])
    for cls in ("ConfirmDialog", "_InstancePickDialog", "InputDialog"):
        a = d_src.index("class %s" % cls)
        b = min([x for x in [d_src.find("class ", a + 10)] if x > 0] or [len(d_src)])
        check(f"{cls} 使用滚动 body",
              "self.body" in d_src[a:b] and "self.make_footer" in d_src[a:b])

    master = ui_smoke.W()
    d = dlgmod._DownloadTargetDialog(master, "MyWorld", "ATM9",
                                     ["ATM9", "ATM10"], {"ATM9": "/tmp/a"},
                                     "/tmp/a/MyWorld")
    check("下载弹窗构造无异常", d is not None)
    check("下载弹窗内容区有控件", len(d.body.kids) >= 5, str(len(d.body.kids)))
    check("底部按钮是窗口直属子控件（滚动时不会被卷走）",
          any(any("开始下载" in str(getattr(k, "_text", ""))
                  for k in _walk(ch)) for ch in d.kids if ch is not d.body),
          str([getattr(k, "_text", "") for k in d.kids]))
    check("底部栏有取消与开始下载",
          any("开始下载" in str(getattr(w, "_text", "")) for w in _walk(d)) and
          any("取消" in str(getattr(w, "_text", "")) for w in _walk(d)))
    d._close()

    # ================================================================ 4
    print("\n[4] 写死像素全部改为动态换行 / 按屏幕收缩")
    from ui import design as ds
    check("design 提供 auto_wrap_label", hasattr(ds, "auto_wrap_label"))
    check("design 提供 auto_wrap_tree", hasattr(ds, "auto_wrap_tree"))
    check("auto_wrap_label 绑定父容器 Configure",
          'bind("<Configure>", upd, add="+")' in inspect.getsource(ds.auto_wrap_label))
    check("主窗口调用 auto_wrap_tree", "ds.auto_wrap_tree(self)" in src_mw)
    for rel in ("client/ui/playerdata_page.py", "client/ui/prune_page.py"):
        check(f"{os.path.basename(rel)} 调用 auto_wrap_tree",
              "auto_wrap_tree(self)" in src(rel))
    check("widgets.py 不再有写死 wraplength",
          "wraplength=" not in src("client/ui/widgets.py"))
    check("主窗口 minsize 按屏幕收缩",
          "self.minsize(min(880, max(640, sw - 120))" in src_mw)
    main_src = src("client/main.py")
    check("紧急窗口不再写死 520x300", 'root.geometry("520x300")' not in main_src)
    check("紧急窗口允许调整大小", "root.resizable(True, True)" in main_src)
    check("紧急窗口按内容与屏幕自适应",
          "winfo_reqheight" in main_src and "sh - 140" in main_src)
    check("紧急窗口底栏固定在窗口底部", 'bar.pack(side="bottom"' in main_src)

    # ================================================================ 5
    print("\n[5] 自检发现的隐藏 bug：设置页保存会崩")
    from ui.dialogs import SettingsDialog
    src_sync = inspect.getsource(SettingsDialog._build_sync)
    check("_build_sync 补回了 v_auto", "self.v_auto =" in src_sync)
    check("_build_sync 补回了 e_auto", "self.e_auto =" in src_sync)
    check("_build_sync 补回了 v_running", "self.v_running =" in src_sync)
    src_save = inspect.getsource(SettingsDialog._save)
    check("_save 引用的三个控件都已存在",
          all(k in src_sync for k in ("self.v_auto", "self.e_auto", "self.v_running")) and
          all(k in src_save for k in ("self.v_auto", "self.e_auto", "self.v_running")))
    check("文件末尾死代码已清除",
          d_src.rstrip().endswith("self._close()") and
          "r += 1\n\n        self.v_running" not in d_src)
    check("设置页调用 _register_wraps", "self._register_wraps()" in d_src)

    # 真实构造 SettingsDialog 并点保存，验证不再 AttributeError
    cfgmod = sys.modules["core.config"]
    dlgmod.cfg = cfgmod.cfg
    st = SettingsDialog(master)
    try:
        st._save()
        check("设置保存不再抛 AttributeError", True)
    except Exception as e:
        check("设置保存不再抛 AttributeError", False, f"{type(e).__name__}: {e}")
    check("自动处理开关配置仍在", "auto_fix_playerdata" in src_save)

    # ================================================================ 6
    print("\n[6] i18n 覆盖")
    import ast
    from core import i18n
    vals = set()
    for rel in ("client/ui/main_window.py", "client/ui/dialogs.py",
                "client/ui/widgets.py", "client/ui/login_window.py",
                "client/ui/prune_page.py", "client/ui/playerdata_page.py",
                "client/main.py"):
        for n in ast.walk(ast.parse(src(rel))):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "t"
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str) and n.args[0].value):
                vals.add(n.args[0].value)
    miss = sorted(v for v in vals if v not in i18n._EN)
    check("界面文案 100% 有英译", not miss, f"{len(vals)} 条，缺 {len(miss)}: {miss[:3]}")

    print("\n" + "=" * 70)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — UUID 修复按钮可用，各页面不再显示不全")
    print("=" * 70)


def _walk(w):
    yield w
    try:
        for ch in w.winfo_children():
            yield from _walk(ch)
    except Exception:
        pass


if __name__ == "__main__":
    main()
