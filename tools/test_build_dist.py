# -*- coding: utf-8 -*-
"""
构建产物占用处理回归测试

场景：Windows 上正在运行的 exe 无法删除，PyInstaller 写产物前
os.remove 会抛 PermissionError [WinError 5]，整个构建失败。
prepare_dist() 必须能处理这种情况。

用法： python3 tools/test_build_dist.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def load_module(path, name="mod"):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    print("\n=== 构建产物占用处理测试 ===\n")
    build = load_module(os.path.join(ROOT, "client", "build.py"), "build_py")
    dist_dir = build.dist_dir if hasattr(build, "dist_dir") else os.path.join(build.HERE, "dist")

    print("[1] 基本属性")
    check("有 prepare_dist", callable(getattr(build, "prepare_dist", None)), "")
    check("有 _running_processes", callable(getattr(build, "_running_processes", None)), "")

    print("\n[2] 无旧产物时直接放行")
    shutil.rmtree(dist_dir, ignore_errors=True)
    check("dist 为空时返回 True", build.prepare_dist() is True, "")

    print("\n[3] 旧产物可删时正常删除")
    os.makedirs(dist_dir, exist_ok=True)
    exe = os.path.join(dist_dir, f"{build.NAME}.exe")
    with open(exe, "wb") as f:
        f.write(b"old")
    check("删除旧产物成功", build.prepare_dist() is True, "")
    check("旧产物确实已移除", not os.path.exists(exe), "")

    print("\n[4] 旧产物被占用（不可删）时改名为 .old")
    with open(exe, "wb") as f:
        f.write(b"locked")
    orig_remove = os.remove
    def fake_remove(p, *a, **k):
        if p == exe:
            raise PermissionError(5, "拒绝访问")
        return orig_remove(p, *a, **k)
    os.remove = fake_remove
    try:
        r = build.prepare_dist()
    finally:
        os.remove = orig_remove
    check("占用时仍返回 True（不阻断构建）", r is True, str(r))
    check("exe 名已让出（原路径不存在）", not os.path.exists(exe), "")
    olds = [f for f in os.listdir(dist_dir) if f.startswith(f"{build.NAME}.exe.old")]
    check("已生成 .old 备份", len(olds) >= 1, str(os.listdir(dist_dir)))
    check(".old 里保留了旧内容",
          any(open(os.path.join(dist_dir, o), "rb").read() == b"locked" for o in olds), "")

    print("\n[5] 连改名都不行时明确报错中止")
    with open(exe, "wb") as f:
        f.write(b"x")
    orig_rename = os.rename
    def fake_rename(a, b, **k):
        if a == exe:
            raise OSError("busy")
        return orig_rename(a, b, **k)
    os.remove = fake_remove
    os.rename = fake_rename
    try:
        r = build.prepare_dist()
    finally:
        os.remove = orig_remove
        os.rename = orig_rename
    check("无法处理时返回 False", r is False, str(r))

    print("\n[6] 进程检测不抛异常")
    try:
        build._running_processes("MCSync.exe")
        check("进程检测可安全调用", True, "")
    except Exception as e:
        check("进程检测可安全调用", False, str(e))

    print("\n[7] 导入自检的缺失分类")
    # GUI 依赖缺失只提示、不阻断；其他缺失必须中止
    gui = ("tkinter", "customtkinter", "PIL", "_tkinter")
    # 直接构造 ast.literal_eval 能解析的合法 repr（等价于子进程真实输出）
    import ast as _ast
    payload = [("ui.dialogs", 'ModuleNotFoundError("No module named \'tkinter\'")'),
               ("core.api", 'ModuleNotFoundError("No module named \'requests\'")')]
    line = "MISSING " + repr(payload)
    parsed = build._parse_missing(line)
    check("能解析出缺失列表", len(parsed) == 2, str(parsed))
    soft = [x for x in parsed if any(g in x[1] for g in gui)]
    fatal = [x for x in parsed if not any(g in x[1] for g in gui)]
    check("GUI 缺失归为软性", [x[0] for x in soft] == ["ui.dialogs"], str(soft))
    check("真缺模块归为致命", [x[0] for x in fatal] == ["core.api"], str(fatal))
    check("解析坏输入不抛异常", build._parse_missing("garbage") == [], "")

    shutil.rmtree(dist_dir, ignore_errors=True)
    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 产物占用可处理")
    print("=" * 58)


if __name__ == "__main__":
    main()
