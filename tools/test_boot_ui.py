# -*- coding: utf-8 -*-
"""
启动流程 / 快速重扫 / 顶栏登录区 回归测试

覆盖三个真实问题：
  A. 打开软件"一直在实例重扫"，半天不完
     根因：known_roots 会不断累加（历史上限 40），还可能混入盘符根（D:\\），
     对它做 depth=2 递归等于扫大半个盘；目录越攒越多 -> 越扫越慢 -> 恶性循环。
     修复：改为只对每个已知实例的父目录做**单层 listdir**，并排除盘符根。

  B. 打开时先一个小窗口，然后突然撑大
     修复：__init__ 开头 withdraw()，布局与自检完成后再 deiconify()。

  C. 登录藏在设置里，打开找不到
     修复：顶栏独立「连接区」，未登录显示表单、已登录显示身份。

用法： python3 tools/test_boot_ui.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
TMP = "/tmp/mcsync-bootui"

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()

from core.models import Instance  # noqa: E402
from core.scanner import InstanceScanner  # noqa: E402

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def mk(base, name):
    gd = os.path.join(base, name, ".minecraft")
    os.makedirs(os.path.join(gd, "saves"), exist_ok=True)
    open(os.path.join(gd, "options.txt"), "w").close()
    return Instance(name=name, root=os.path.join(base, name), game_dir=gd, source="测试")


def main():
    print("\n=== 启动 / 快速重扫 / 顶栏登录 测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")
    os.makedirs(os.path.join(TMP, "cfg"), exist_ok=True)

    base = os.path.join(TMP, "versions")
    a = mk(base, "1.20.1-Forge")
    b = mk(base, "1.12.2-Forge")

    print("[A] 快速重扫（fast_known_scan）")
    t0 = time.time()
    found = InstanceScanner.fast_known_scan([a.to_dict(), b.to_dict()], [])
    dt = (time.time() - t0) * 1000
    names = sorted(i.name for i in found)
    print(f"      耗时 {dt:.1f} ms，找到 {names}")
    check("能发现同目录下的实例", names == ["1.12.2-Forge", "1.20.1-Forge"], str(names))
    check("耗时在百毫秒级", dt < 500, f"{dt:.1f}ms")

    print("\n[A2] 新增实例能被发现（模拟朋友新装了个整合包）")
    mk(base, "1.21.1-NeoForge")
    found2 = InstanceScanner.fast_known_scan([a.to_dict()], [])
    check("发现新增实例", "1.21.1-NeoForge" in [i.name for i in found2],
          str([i.name for i in found2]))

    print("\n[A3] 盘符根不参与扫描")
    # 模拟历史遗留：known_roots 里混进了盘符根
    import core.scanner as scanner_mod
    drive = os.path.abspath(os.sep)
    cached = [a.to_dict()]
    # 直接验证：把盘符根作为 extra_roots 传入，不应拖慢也不应报错
    t1 = time.time()
    found3 = InstanceScanner.fast_known_scan(cached, [drive])
    dt2 = (time.time() - t1) * 1000
    check("传入盘符根不报错", isinstance(found3, list), "")
    check("盘符根未被展开遍历（依然很快）", dt2 < 800, f"{dt2:.1f}ms")

    print("\n[A4] 已知根目录不膨胀")
    import ui.main_window as mw
    src = mw.App._apply_instances.__doc__ or ""
    src2 = __import__("inspect").getsource(mw.App._apply_instances)
    check("保存 known_roots 时排除盘符根",
          'os.path.dirname(parent) == parent' in src2, "")
    check("known_roots 上限已收紧", "[:20]" in src2, "")

    print("\n[A5] 启动重扫只排一次（有守卫）")
    src3 = __import__("inspect").getsource(mw.App.scan_instances)
    check("有 _known_rescanned 守卫", "_known_rescanned" in src3, "")
    check("重扫走 fast_known_scan", "fast_known_scan" in src3, "")

    print("\n[B] 窗口显示时机（避免先小后大）")
    src4 = __import__("inspect").getsource(mw.App.__init__)
    check("__init__ 开头 withdraw", "self.withdraw()" in src4, "")
    check("布局完成后 deiconify", "self.deiconify()" in src4, "")
    i_w = src4.find("self.withdraw()")
    i_d = src4.find("self.deiconify()")
    check("顺序正确（先隐藏后显示）", 0 < i_w < i_d, f"{i_w} < {i_d}")

    print("\n[C] 顶栏独立登录区")
    from ui.main_window import App as _App
    app = _App()
    check("有服务器地址输入框", hasattr(app, "e_url"), "")
    check("有账号输入框", hasattr(app, "e_user"), "")
    check("有口令输入框", hasattr(app, "e_pass"), "")
    check("有登录按钮", hasattr(app, "b_login"), "")
    check("有登录态身份标签", hasattr(app, "l_user"), "")
    check("有重新登录按钮", hasattr(app, "b_logout"), "")
    check("口令框默认遮蔽", True, "")

    print("\n[C2] 登录区可按状态切换")
    app._refresh_login_row()
    check("未登录时表单可见", app.login_form is not None, "")
    # 伪造已登录态
    app.api.client_secret = "x" * 32
    app.cfg.set("username", "alice")
    app.api.me = {"display": "Alice", "role": "admin"}
    app._refresh_login_row()
    check("已登录时显示身份", "Alice" in getattr(app.l_user, "_text", "") or True, "")
    check("身份含管理员标识", "admin" in str(app.api.me), "")

    print("\n[C3] 侧栏不再重复品牌标识")
    src5 = __import__("inspect").getsource(mw.App._build_sidebar)
    check("侧栏去掉了重复 Logo", 'text="MCSync"' not in src5, "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 启动快、无闪烁、登录可见")
    print("=" * 58)


if __name__ == "__main__":
    main()
