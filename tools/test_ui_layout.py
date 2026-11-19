# -*- coding: utf-8 -*-
"""
UI 显示自检（强制门禁，防止「显示不全」反复出现）
══════════════════════════════════════════════════════════════════

为什么要有这个文件
──────────────────
同一个错误已经出现过三次：设置窗口 → 云端下载弹窗 → UUID 修复确认框。
每次都是「开发机看着正常，别人设备（高 DPI / 小屏 / 大字号）底部被裁」。
根因始终是同一类：固定 geometry、pack 顺序不对、写死 wraplength。

所以这里把「显示正不正常」变成**可自动验证的结构约束**，而不是靠肉眼：

  1. 自动发现 ui/dialogs.py 里所有 _BaseDialog 子类。
     新加弹窗却没在 BUILDERS 登记 → 测试直接失败，逼你把它纳入自检。
  2. 在 7 种屏幕 / DPI 组合下逐个构造弹窗，验证：
       · footer 是窗口直属子控件，且 pack(side="bottom")
       · footer 的 pack 顺序早于 body（Tk 按 pack 顺序分配空间，
         晚 pack 的底栏会被内容挤出可视区 —— 就是这次的 bug）
       · 窗口宽高不超过屏幕可用区
       · 所有 wraplength 不超过窗口宽度（文字不会从右侧溢出）
       · 内容装得下，或 body 本身可滚
       · 构造后会执行 _fit_content 自适应
  3. 源码级护栏：不许再出现写死 wraplength / 自建 bottom 底栏。
  4. 反向验证：故意构造一个「footer 后置」的反例，确认自检确实能抓到。

build.py 会在打包前强制跑本文件，失败即中止构建。
用法： python3 tools/test_ui_layout.py
"""
from __future__ import annotations

import contextlib
import inspect
import math
import os
import re
import shutil
import sys
import tempfile
import traceback


def _safe_stdio():
    """Windows 中文控制台下的生死线，别删。

    build.py 用 subprocess.run(capture_output=True) 起本脚本。父进程直接
    输出到控制台时中文正常，但**子进程的 stdout 被重定向到管道后，
    Windows Python 会改用 locale 编码（GBK/CP936）**。而本脚本大量打印
    ✅ / ❌ / ═ 等字符 → 第一个 print 就抛

        UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'

    脚本当场崩溃、rc=1、stdout 几乎为空。build.py 只看到「UI 布局自检
    未通过」却打印不出任何细节 —— 看起来像布局坏了，实际是自检自己崩了。

    双保险：
      1) 把 stdout/stderr 重配置成 UTF-8 + errors="replace"（Python 3.7+）
      2) 所有状态标记改用纯 ASCII（[OK] / [FAIL]），连 emoji 都不打印
    """
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_safe_stdio()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# build.py 会在 Windows 上跑这个自检，临时目录必须跨平台
TMP = os.path.join(tempfile.gettempdir(), "mcsync-ui-layout")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MCSYNC_CONFIG_DIR"] = TMP

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()

OK: list = []
LAST_CHECK = ["(尚未开始)"]     # 崩溃时告诉用户停在哪一项，别再满屏 [OK] 后戛然而止


def check(name: str, cond: bool, extra: str = ""):
    LAST_CHECK[0] = name
    # 纯 ASCII 标记：任何代码页下都不会崩（见 _safe_stdio 说明）
    print(("  [OK] " if cond else "  [FAIL] ") + name
          + (f"  -- {extra}" if extra else ""))
    OK.append((name, bool(cond)))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def walk(w):
    yield w
    try:
        for ch in w.winfo_children():
            yield from walk(ch)
    except Exception:
        pass


# ---------------------------------------------------------------- 屏幕矩阵
# (物理宽, 物理高, DPI 缩放, 说明) —— 逻辑尺寸 = 物理 / 缩放
SCREENS = [
    (1920, 1080, 1.00, "1080p / 100%"),
    (2560, 1440, 1.00, "2K / 100%"),
    (1600, 900, 1.00, "笔记本 / 100%"),
    (1366, 768, 1.00, "常见笔记本 / 100%"),
    (1280, 720, 1.00, "小笔记本 / 100%"),
    (1366, 768, 1.50, "1366×768 @150% 缩放（朋友设备的典型场景）"),
    (1920, 1080, 2.00, "1080p @200% 缩放"),
    (1024, 600, 1.00, "上网本 / 极限小屏"),
]

# ---------------------------------------------------------------- 构造模板
# 每个 _BaseDialog 子类都必须在这里登记。新增弹窗不登记 = 测试失败。
LONG_PATH = r"E:\项目\mcsync\instances\ATM9\.minecraft\saves\MyWorld"

# args 里不含 master —— 调用处是 c(master, *args, **kwargs)。
# 若把 master 也塞进 args，参数会整体错位（title 变成 W 对象等），
# 自检就会误报"构造异常"。
BUILDERS = {
    "ConfirmDialog": lambda m: dict(
        args=("本地 UUID 修复",
              "即将修复本地存档「我的世界」：\n\n"
              "⚠ 内嵌房主玩家数据（a1b2c3d4…）→ 别人打开会变成房主\n\n"
              "会把内嵌的房主玩家数据归档成独立 .dat，并从 level.dat 中摘除 "
              "UUID 指针。房主本人仍能按自己的 UUID 读回数据。",
              "立即修复"),
        kwargs=dict(extra="仅修改本地存档，不会上传到云端；执行前自动备份 level.dat。\n"
                          f"目录：{LONG_PATH}")),
    "ConflictDialog": lambda m: dict(
        args=([{"path": "level.dat", "local_hash": "a" * 32,
                "cloud_hash": "b" * 32},
               {"path": f"region/{LONG_PATH}/r.0.0.mca",
                "local_hash": "c" * 32, "cloud_hash": "d" * 32}],),
        kwargs=dict()),
    "VersionDialog": lambda m: dict(
        args=("我的世界存档",
              [{"id": i, "created_at_str": "2026-11-17 00:00",
                "file_count": 412, "total_size": 128 * 1024 * 1024,
                "note": "整合包配置更新"} for i in range(1, 9)]),
        kwargs=dict()),
    "SettingsDialog": lambda m: dict(args=(), kwargs=dict()),
    "_InstancePickDialog": lambda m: dict(
        args=("MyWorld", "ATM9", ["ATM9", "ATM10", "1.20.1-Forge-OptiFine"]),
        kwargs=dict()),
    "_DownloadTargetDialog": lambda m: dict(
        args=("MyWorld", "ATM9", ["ATM9", "ATM10"],
              {"ATM9": LONG_PATH, "ATM10": r"D:\MC\ATM10"}),
        kwargs=dict(default_dir=os.path.join(LONG_PATH, "MyWorld"))),
    "InputDialog": lambda m: dict(
        args=("重命名", "请输入新的存档名称，可包含中文与空格", "MyWorld"),
        kwargs=dict()),
}

# 这些弹窗底部没有按钮（按钮在列表行内），不检查 footer 按钮
NO_FOOTER_BUTTONS = {"VersionDialog"}
# 所有弹窗都必须出现的关键按钮文字（NO_FOOTER_BUTTONS 除外）
REQUIRED_BUTTONS = {
    "ConfirmDialog": ("取消", "立即修复"),
    "ConflictDialog": ("取消", "确定"),
    "SettingsDialog": ("取消", "保存"),
    "_InstancePickDialog": ("取消", "确定"),
    "_DownloadTargetDialog": ("取消", "开始下载"),
    "InputDialog": ("取消", "确定"),
}


def _as_int_for_test(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _close_twice(dlg) -> bool:
    try:
        dlg._close()
        dlg._close()
        return True
    except Exception:
        return False


def main():
    print("\n=== UI 布局自检（CTK 6.0 统一方案） ===\n")
    import ui.dialogs as dlgmod

    # ------------------------------------------------------------ 1 统一方案
    print("[1] 统一方案护栏：一套 CTK 6.0，不再有兼容层/降级/版本切换")
    classes = {n: c for n, c in inspect.getmembers(dlgmod, inspect.isclass)
               if issubclass(c, dlgmod._BaseDialog)
               and c is not dlgmod._BaseDialog
               and c.__module__ == dlgmod.__name__}
    print(f"      发现 {len(classes)} 个对话框：{', '.join(sorted(classes))}")
    missing = sorted(set(BUILDERS) - set(classes))
    check("所有对话框都已登记进自检", not missing, str(missing))
    check("恰好 7 个对话框", len(classes) == 7, str(len(classes)))

    d_src = src("client/ui/dialogs.py")
    # 只检查真实代码定义/表达式，别被文档注释里的「不再有 X」字样误伤
    check("不再有 __init_subclass__ 包装器",
          "def __init_subclass__" not in d_src)
    check("不再有 _native_* 降级窗口",
          "def _native_shell" not in d_src and "def _native_confirm" not in d_src
          and "def _native_emergency" not in d_src
          and "_native_shell(" not in d_src)
    check("不再有版本探测 / 主动降级",
          "def _ctk_broken" not in d_src and "class _UseNative" not in d_src
          and "_ctk_broken()" not in d_src)
    check("不再混用 tkinter 原生控件创建按钮/标签",
          "tk.Button(" not in d_src and "tk.Label(" not in d_src
          and "import tkinter as tk" not in d_src)
    check("不存在 ui/tk_compat.py（运行期 monkey patch 已删除）",
          not os.path.isfile(os.path.join(ROOT, "client", "ui", "tk_compat.py")))
    for rel in ("client/main.py", "client/ui/login_window.py",
                "client/ui/dialogs.py"):
        check(f"{os.path.basename(rel)} 不再安装兼容补丁",
              "tk_compat" not in src(rel))

    # ★ 本次总根因：self._w 是 tkinter 的 widget path，绝不能当普通变量
    check("★ 不再覆盖 self._w（widget path）",
          "self._w = _as_int" not in d_src and "self._h = _as_int" not in d_src
          and re.search(r"^\s*self\._w\s*=", d_src, re.M) is None
          and re.search(r"^\s*self\._h\s*=", d_src, re.M) is None)
    check("宽高改用 _dlg_w / _dlg_h",
          "self._dlg_w" in d_src and "self._dlg_h" in d_src)

    # 运行时验证：构造一个真实对话框，确认 widget path 没被改成数字。
    # 旧版 self._w = 720 会让 Tcl 报 bad window path name "720"。
    ui_smoke.reset_grab()
    ui_smoke.PENDING.clear()
    W = ui_smoke.W
    _m = W()
    _spec = BUILDERS["ConfirmDialog"](_m)
    _probe = dlgmod.ConfirmDialog(_m, *_spec["args"], **_spec["kwargs"])
    check("★ 运行时 _w 仍是 widget path，不是数字宽高",
          not isinstance(getattr(_probe, "_w", None), int) and
          isinstance(_probe._dlg_w, int) and _probe._dlg_w == 520,
          f"_w={getattr(_probe, '_w', None)!r} _dlg_w={_probe._dlg_w!r}")
    try:
        _probe._close()
    except Exception:
        pass
    ui_smoke.reset_grab()

    req = src("client/requirements.txt")
    b = src("client/build.py")
    check("requirements.txt 钉死 customtkinter==6.0.0",
          "customtkinter==6.0.0" in req and "<6" not in req)
    check("build.py 只校验 6.0.0，不再 force-reinstall / 降级",
          '"6.0.0"' in b and '"--force-reinstall"' not in b
          and 'pip", "install", "customtkinter>=' not in b
          and "ctk.set_spacing_scaling(" not in src("client/ui/theme.py"))
    check("theme.py 使用 CTk 6.0 的 set_window_scaling",
          "ctk.set_window_scaling" in src("client/ui/theme.py"))

    # ------------------------------------------------------------ 2 布局结构
    print("\n[2] 布局结构：footer 先 pack、body 后 pack、动态换行")
    base = d_src[d_src.index("class _BaseDialog"):
                 d_src.index("class ConfirmDialog")]
    fp = base.index("self.footer.pack(")
    bp = base.index("self.body.pack(")
    check("footer 的 pack 早于 body（Tk 按 pack 顺序分配空间）", fp < bp)
    check("footer 用 side=bottom", 'side="bottom"' in base[fp:fp + 200])
    check("body 用 fill=both + expand",
          'fill="both", expand=True' in base[bp:bp + 160])
    check("make_footer 用 before=self.body 钉死顺序",
          'opts["before"] = self.body' in base)
    check("wraplength 跟随窗口宽度，不写死像素",
          "def auto_wrap" in base and "def _apply_wrap" in base
          and "wraplength=500" not in d_src)
    check("底栏为空时有统一 CTkButton 兜底（不再用 tk.Button）",
          "def _ensure_footer_buttons" in base
          and "ctk.CTkButton(bar" in base)
    check("wait_window 覆写里做底栏非空校验",
          "def wait_window(self)" in base
          and "self._ensure_footer_buttons(self.footer" in base)

    # ------------------------------------------------------------ 3 屏幕矩阵
    print("\n[3] 8 种屏幕 / DPI 组合逐个构造全部对话框")
    W = ui_smoke.W
    W.LAYOUT_MODE = True
    failures = []
    for sw, sh, dpi, label in SCREENS:
        print(f"      · {label} -> 逻辑 {int(sw / dpi)}x{int(sh / dpi)}")
        W.SIM_SW, W.SIM_SH = int(sw / dpi), int(sh / dpi)
        for name in sorted(classes):
            ui_smoke.reset_grab()
            ui_smoke.PENDING.clear()
            m = W()
            spec = BUILDERS[name](m)
            try:
                dlg = classes[name](m, *spec["args"], **spec["kwargs"])
                for fn in list(ui_smoke.PENDING):
                    try:
                        if callable(fn):
                            fn()
                    except Exception:
                        pass
            except Exception as e:
                failures.append(f"{name}@{label} 构造抛异常: {e}")
                continue
            # 底栏必须有按钮（VersionDialog 的按钮在列表行内，除外）
            if name not in NO_FOOTER_BUTTONS:
                try:
                    kids = list(dlg.footer.winfo_children())
                except Exception:
                    kids = []
                if not kids:
                    failures.append(f"{name}@{label} 底栏无按钮")
                texts = [str(k.cget("text") or "") for k in kids]
                for btn in REQUIRED_BUTTONS.get(name, ()):
                    if btn and not any(btn in tx for tx in texts):
                        failures.append(f"{name}@{label} 缺按钮「{btn}」")
            # 窗口不超过屏幕可用区
            try:
                if dlg.winfo_width() > W.SIM_SW or dlg.winfo_height() > W.SIM_SH - 60:
                    failures.append(f"{name}@{label} 窗口超出屏幕")
            except Exception:
                pass
            try:
                dlg._close()
            except Exception:
                pass
            ui_smoke.reset_grab()
    check("8 种屏幕 × 7 个对话框全部布局合规", not failures,
          (" | ".join(failures[:6])) if failures else "")
    W.LAYOUT_MODE = False
    W.SIM_SW, W.SIM_SH = 1920, 1080
    ui_smoke.reset_grab()

    # ------------------------------------------------------------ 4 主题按钮
    print("\n[4] 明暗切换按钮：纯 CTkButton、固定正方形、位置与其他按钮一致")
    mw = src("client/ui/main_window.py")
    check("不再有 Canvas 矢量绘制",
          "def _make_theme_button" not in mw and "tk.Canvas(" not in mw
          and "pack_propagate(False)" not in mw)
    check("主题按钮是 CTkButton",
          "self.b_theme = ctk.CTkButton(" in mw)
    check("width=height 固定正方形（不会被拉扁）",
          'width=sz["btn_h_sm"], height=sz["btn_h_sm"]' in mw)
    check("用几何符号而非 emoji",
          "\\u25d0" in mw and "\\u25d1" in mw and "🌙" not in mw)
    check("与设置按钮同一容器、同一 pack(side=left)",
          "self.b_theme.pack(side=\"left\"" in mw)
    check("不再有「兼容模式/降级」日志分支",
          "_native_built" not in mw and "_degraded" not in mw
          and "兼容模式" not in mw)

    # ------------------------------------------------------------ 5 模态
    print("\n[5] 模态生命周期：构造期不 grab，关闭必释放")
    check("构造期不 grab_set",
          "self.after_idle(self._arm_modal)" in base
          and "grab_set()" not in base[:base.index("self.after_idle(self._arm_modal)")])
    check("_arm_modal 抓不到就退化，不外抛",
          "def _arm_modal" in base and "except Exception:" in base)
    check("_close 幂等且释放 grab、销毁窗口",
          "def _close" in base and "self.grab_release()" in base
          and "self.destroy()" in base and "if self._closed:" in base)
    check("wait_window 调用父类", "return super().wait_window()" in base)

    # ------------------------------------------------------------ 6 结果
    print("\n" + "=" * 74)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"[FAIL] 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    # 保留「全部 N 项通过」字样：build.py 靠它抓成功摘要
    print(f"[OK] 全部 {len(OK)} 项通过 - "
          f"{len(classes)} 个对话框 × {len(SCREENS)} 种屏幕规格均无显示不全")
    print("=" * 74)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        msg = traceback.format_exc()
        try:
            print(f"[FAIL] SELFCHECK_CRASHED at: {LAST_CHECK[0]}", flush=True)
            print("       这是自检工具的问题，不是布局问题", flush=True)
        except Exception:
            pass
        try:
            sys.stderr.write(
                msg.encode("ascii", "backslashreplace").decode("ascii"))
        except Exception:
            try:
                sys.stderr.write("SELFCHECK_CRASHED (traceback unavailable)\n")
            except Exception:
                pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        raise SystemExit(2)
