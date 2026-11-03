# -*- coding: utf-8 -*-
"""
登录窗口回归测试

覆盖：
  - 凭据齐全且允许自动登录 → 后台自动登录，成功直接进主界面
  - 未勾"记住口令" → 不保存口令
  - 登录失败 → 退回表单并提示，不静默卡住
  - 选择"稍后" → 也能离线使用本地功能
  - 口令在磁盘上不以明文存在

用法： python3 tools/test_login_window.py
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
TMP = "/tmp/mcsync-login"

import ui_smoke  # noqa: E402  安装 tkinter 桩件
ui_smoke.install_stubs()

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def main():
    print("\n=== 登录窗口回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "cfg"), exist_ok=True)
    os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")

    from core.config import cfg

    print("[1] 自动登录的前置条件判断")
    # 无凭据时不应触发自动登录
    cfg.set("server_url", "")
    cfg.set("username", "")
    cfg.set_secret("", "password")
    cfg.set("remember_password", True)
    cfg.set("auto_login", True)

    import ui.login_window as lw
    check("登录窗口模块可导入", hasattr(lw, "LoginWindow"), "")

    src = open(os.path.join(ROOT, "client", "ui", "login_window.py"),
               encoding="utf-8").read()
    # 自动登录必须做前置检查：缺任一项就不该发起网络请求
    for cond in ('cfg.get("server_url"', 'cfg.get("username"',
                 'cfg.get_secret("password")', 'cfg.get("remember_password"',
                 'cfg.get("auto_login"'):
        check(f"自动登录检查 {cond}", cond in src, "")

    print("\n[2] 登录成功后按「记住我」决定口令存留")
    check("成功路径保存口令", 'self.v_remember.get()' in src, "")
    check("记住时写入加密区", 'cfg.set_secret(pw, "password")' in src, "")

    # set_secret 的签名是 (value, name)。曾经在这里把名字写成了第一个参数，
    # 结果口令被存进名为口令值的键里 —— 读回来是空，自动登录永远失败。
    # 这里用 AST 全项目扫描，防止再犯。
    import ast
    order_bad = []
    for rel in ("client/core/api.py", "client/core/config.py",
                "client/ui/dialogs.py", "client/ui/main_window.py",
                "client/ui/login_window.py"):
        fp = os.path.join(ROOT, rel)
        tree = ast.parse(open(fp, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "set_secret" and len(n.args) == 2:
                a0 = n.args[0]
                if isinstance(a0, ast.Constant) and a0.value in ("password", "client_secret"):
                    order_bad.append((rel, n.lineno))
    check("set_secret 参数顺序全项目正确", not order_bad, str(order_bad))
    check("不记住时清除口令", 'cfg.set_secret("", "password")' in src, "")
    check("不记住时关闭 remember 标记",
          'cfg.set("remember_password", False)' in src, "")

    print("\n[3] 登录失败的处理")
    # 自动登录失败必须静默退回表单，而不是弹错误吓用户
    check("自动登录失败退回表单", "if is_auto:" in src and "self.e_pass.focus_set()" in src, "")
    check("手动登录失败显示错误", "self._msg(err, True)" in src, "")
    check("失败后恢复按钮可点", "_set_busy(False)" in src, "")

    print("\n[4] 「稍后」与关闭窗口")
    check("有稍后入口（离线可用）", "_skip" in src, "")
    check("关闭窗口也走同一收尾", "_on_close" in src and "_finish" in src, "")
    check("收尾会释放 grab", "grab_release" in src, "")

    print("\n[5] 口令不落明文")
    cfg.set_secret("Pw-123456", "password")
    check("可存可取", cfg.get_secret("password") == "Pw-123456", "")
    cfg.save()
    raw = open(cfg.path, encoding="utf-8").read() if os.path.exists(cfg.path) else ""
    check("配置文件里无明文口令", "Pw-123456" not in raw, "")

    print("\n[6] 主窗口已接入登录回调")
    mw_src = open(os.path.join(ROOT, "client", "ui", "main_window.py"),
                  encoding="utf-8").read()
    check("主窗口有 on_login_result", "def on_login_result" in mw_src, "")
    check("启动走 LoginWindow", "LoginWindow(on_success=after_login" in mw_src, "")
    check("登录后才显示主窗口", "app.deiconify()" in mw_src, "")
    check("主窗口构建时不自行显示（避免闪一下）",
          mw_src.count("self.deiconify()") == 0, "应不在 __init__ 里 deiconify")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 登录流程可用且凭据安全")
    print("=" * 58)


if __name__ == "__main__":
    main()
