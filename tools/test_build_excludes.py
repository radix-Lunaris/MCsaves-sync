# -*- coding: utf-8 -*-
"""
打包排除项安全回归测试

背景（真实事故）：为"瘦身"凭直觉排除了 email 等标准库模块，
结果 urllib3 依赖 email（HTTP 头解析走 email.parser / email.utils），
打包出的 exe 能正常生成，一运行就报
    ModuleNotFoundError: No module named 'email'
而这类错误只有在用户运行时才暴露。

本测试做的事：
  1. 解析 build.py 里真实的 --exclude-module 列表
  2. 静态扫描第三方依赖（requests / urllib3 / PIL / ...）实际 import 的顶层模块
  3. 两者求交集 —— 有交集就是致命排除，直接测失败
  4. 校验 NEVER_EXCLUDE 黑名单确实生效

用法： python3 tools/test_build_excludes.py
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def load_build():
    spec = importlib.util.spec_from_file_location(
        "build_py", os.path.join(ROOT, "client", "build.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_excludes(path: str) -> list:
    """从 build.py 源码里取出真实的 --exclude-module 列表。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.List):
            vals = [e.value for e in n.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            for i, v in enumerate(vals):
                if v == "--exclude-module" and i + 1 < len(vals):
                    out.append(vals[i + 1])
    return sorted(set(out))


def third_party_stdlib_usage() -> dict:
    """扫描第三方包实际 import 的顶层标准库模块。"""
    std = set(sys.stdlib_module_names)
    pkgs = ["requests", "urllib3", "certifi", "charset_normalizer", "idna",
            "xxhash", "PIL", "customtkinter", "darkdetect", "packaging"]
    usage = {}
    for pkg in pkgs:
        try:
            spec = importlib.util.find_spec(pkg)
        except Exception:
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        loc = list(spec.submodule_search_locations)[0]
        uses = set()
        for dp, dns, fs in os.walk(loc):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fs:
                if not fn.endswith(".py"):
                    continue
                try:
                    t = ast.parse(open(os.path.join(dp, fn), encoding="utf-8",
                                       errors="ignore").read())
                except Exception:
                    continue
                for n in ast.walk(t):
                    if isinstance(n, ast.Import):
                        for a in n.names:
                            uses.add(a.name.split(".")[0])
                    elif isinstance(n, ast.ImportFrom):
                        if n.level == 0 and n.module:
                            uses.add(n.module.split(".")[0])
        usage[pkg] = uses & std
    return usage


def main():
    print("\n=== 打包排除项安全测试 ===\n")
    bp = os.path.join(ROOT, "client", "build.py")
    build = load_build()

    print("[1] 解析 build.py 的排除项")
    excl = parse_excludes(bp)
    check("能解析出排除项", len(excl) > 0, str(len(excl)))
    print(f"      共 {len(excl)} 项: {', '.join(excl)}")

    print("\n[2] 扫描第三方依赖的实际标准库引用")
    usage = third_party_stdlib_usage()
    check("能扫到依赖", len(usage) >= 3, str(sorted(usage)))
    total = set().union(*usage.values()) if usage else set()
    print(f"      第三方共引用 {len(total)} 个标准库模块")

    print("\n[3] 排除项不得命中被依赖的模块")
    # 注意：排除子模块 a.b 只会让 a.b 消失，顶层 a 仍在，
    # 所以判定要用"前缀命中"而不是"顶层命中"——
    # 否则排除 tkinter.test 会因为 PIL 用了 tkinter 而被误判成致命。
    hit = {}
    for m in excl:
        srcs = [p for p, u in usage.items()
                if any(x == m or x.startswith(m + ".") for x in u)]
        if srcs:
            hit[m] = srcs
    if hit:
        for m, s_ in hit.items():
            print(f"      ❌ {m} 被依赖：{', '.join(s_)}")
    check("无致命排除项", not hit, str(list(hit)))

    print("\n[4] NEVER_EXCLUDE 黑名单")
    never = getattr(build, "NEVER_EXCLUDE", set())
    check("黑名单存在", len(never) > 0, str(len(never)))
    check("email 在黑名单中", "email" in never, "")
    ok_, bad = build.validate_excludes(
        ["--exclude-module", "email", "--exclude-module", "pydoc"])
    check("validate_excludes 能拦住 email", (not ok_) and bad == ["email"], str(bad))
    ok2, bad2 = build.validate_excludes(["--exclude-module", "pydoc"])
    check("安全排除项放行", ok2 and not bad2, str(bad2))

    print("\n[5] 当前构建命令本身通过校验")
    # 直接构造一条与真实一致的排除项命令验证
    cmd = []
    for m in excl:
        cmd += ["--exclude-module", m]
    ok3, bad3 = build.validate_excludes(cmd)
    check("真实排除项不触发黑名单", ok3, str(bad3))

    print("\n[6] UI 布局自检必须挂在打包门禁上")
    # 「显示不全」反复出现过，靠人眼复查不可靠，必须让 build.bat 每次强制跑
    build_src = open(os.path.join(ROOT, "client", "build.py"), encoding="utf-8").read()
    check("build.py 定义了 preflight_ui_layout",
          "def preflight_ui_layout" in build_src)
    check("build() 调用了它且在 PyInstaller 之前",
          "if not preflight_ui_layout(py, strict=strict_ui):" in build_src and
          build_src.index("if not preflight_ui_layout(py, strict=strict_ui):") <
          build_src.index("rc = run(cmd)"))
    _pf = build_src[build_src.index("def preflight_ui_layout"):
                    build_src.index("def build(")]
    check("★ 只有明确布局断言失败才中止（工具链自身问题放行）",
          "layout_fails" in _pf and _pf.count("return False") == 1 and
          "不是布局问题" in _pf and "已放行继续构建" in _pf)
    check("★ 提供 --no-ui-check 逃生开关并接入 main/build",
          '"--no-ui-check"' in build_src and
          "strict_ui=not args.no_ui_check" in build_src)
    check("★ 自检脚本中断/超时/编码崩溃一律放行",
          "timed_out" in _pf and "self_crash" in _pf and
          _pf.rstrip().endswith("return True"))
    check("门禁失败时区分工具链问题并给排查路径",
          "_ui_layout_last.log" in _pf and "-X utf8" in _pf)
    check("存在 test_ui_gate.py 门禁策略回归",
          os.path.isfile(os.path.join(ROOT, "tools", "test_ui_gate.py")))
    checker = os.path.join(ROOT, "tools", "test_ui_layout.py")
    chk_src = open(checker, encoding="utf-8").read()
    dlg_src = open(os.path.join(ROOT, "client", "ui", "dialogs.py"),
                   encoding="utf-8").read()
    check("布局自检脚本存在且可独立运行",
          os.path.isfile(checker) and "SCREENS" in chk_src)
    check("★ UI 层已按 CTK 6.0 统一重写（无兼容层/降级/版本切换）",
          not os.path.isfile(os.path.join(ROOT, "client", "ui",
                                          "tk_compat.py")) and
          "def __init_subclass__" not in dlg_src and
          "def _native_shell" not in dlg_src and
          "self._w = _as_int" not in dlg_src)
    check("★ requirements 与 build.py 都钉死 customtkinter 6.0.0",
          "customtkinter==6.0.0" in open(
              os.path.join(ROOT, "client", "requirements.txt"),
              encoding="utf-8").read() and
          '"6.0.0"' in build_src and '"--force-reinstall"' not in build_src)
    check("★ 主题按钮是纯 CTkButton 固定正方形（不再 Canvas 矢量）",
          "self.b_theme = ctk.CTkButton(" in open(
              os.path.join(ROOT, "client", "ui", "main_window.py"),
              encoding="utf-8").read() and
          "def _make_theme_button" not in open(
              os.path.join(ROOT, "client", "ui", "main_window.py"),
              encoding="utf-8").read())
    check("★ 自检覆盖 CTK 6.0 统一方案护栏",
          "[1] 统一方案护栏" in chk_src and
          "不再覆盖 self._w" in chk_src and
          "不再有 _native_* 降级窗口" in chk_src)
    check("★ 自检覆盖 8 屏幕 × 7 对话框的按钮存在性",
          "[3] 8 种屏幕" in chk_src and
          "REQUIRED_BUTTONS" in chk_src and
          "底栏无按钮" in chk_src)
    check("★ 自检覆盖主题按钮固定正方形",
          "[4] 明暗切换按钮" in chk_src and
          "width=height 固定正方形" in chk_src)
    check("★ 自检覆盖模态生命周期",
          "[5] 模态生命周期" in chk_src and
          "构造期不 grab" in chk_src)

    print("\n[7] customtkinter 6.0.0 统一标准（不再换版本）")
    req_src = open(os.path.join(ROOT, "client", "requirements.txt"),
                   encoding="utf-8").read()
    check("requirements.txt 钉死 customtkinter==6.0.0",
          "customtkinter==6.0.0" in req_src and "<6" not in req_src)
    check("build.py 定义了 ensure_ctk_version",
          "def ensure_ctk_version" in build_src)
    _fn = build_src[build_src.index("def ensure_ctk_version"):
                    build_src.index("def install_deps")]
    check("只校验 6.0.0，不做 force-reinstall / 降级",
          '"6.0.0"' in _fn and '"--force-reinstall"' not in _fn
          and "5.2.2" not in _fn and "pip", "install" not in _fn)
    check("确认版本读模块 __version__",
          "getattr(c, '__version__'" in _fn)
    check("main 在 install_deps 之后调用版本校验",
          build_src.index("if not ensure_ctk_version(py):") >
          build_src.index("if not install_deps(py):"))
    check("theme.py 改用 CTk 6.0 的 set_window_scaling",
          "ctk.set_window_scaling" in open(
              os.path.join(ROOT, "client", "ui", "theme.py"),
              encoding="utf-8").read())
    check("dialogs.py 不再有 _UseNative / 应急窗口 / 诊断降级字段",
          "class _UseNative" not in dlg_src and
          "def _native_emergency" not in dlg_src and
          "_init_error_diag" not in dlg_src)
    check("make_footer 强制 packing order 并二次自适应",
          'opts["before"] = self.body' in dlg_src and
          "after_idle(self._fit_content)" in dlg_src)
    check("尺寸计算有 footer 硬下限与运行时校验",
          "34 + 2 * 14 + 8" in dlg_src and
          "def _body_height" in dlg_src and
          "def _fit_content" in dlg_src)

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 排除项安全，不会打出跑不起来的 exe")
    print("=" * 58)


if __name__ == "__main__":
    main()
