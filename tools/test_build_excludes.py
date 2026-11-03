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

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 排除项安全，不会打出跑不起来的 exe")
    print("=" * 58)


if __name__ == "__main__":
    main()
