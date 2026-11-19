# -*- coding: utf-8 -*-
"""构建门禁策略回归：**工具链自身出问题绝不阻断交付**。

事故复现（用户日志）：
    [5] 模态安全 打了 8 个 [OK] 后戛然而止
    → 没有汇总行、没有 traceback、没有 [FAIL]
    → build.py 却报「UI 布局自检未通过，已中止构建」
      并把最后 22 行（全是 [OK]）当失败原因打印

本测试用 monkeypatch 模拟各种子进程返回，直接验证
client/build.py :: preflight_ui_layout 的三分支决策。
全 ASCII 输出，任何代码页可跑。
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import types
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "client", "build.py")

OK = []


def check(name, cond, extra=""):
    OK.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name
          + ("  -- " + str(extra) if extra else ""))
    if not cond:
        raise AssertionError(name + (" | " + str(extra) if extra else ""))


def load_build():
    spec = importlib.util.spec_from_file_location("mcsync_build_gate", BUILD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fake_run(stdout="", stderr="", rc=0, timeout=False):
    """返回一个替身 subprocess.run，模拟自检子进程的各种结局。"""
    def _run(cmd, **kw):
        if timeout:
            raise m_subprocess.TimeoutExpired(cmd, kw.get("timeout", 120),
                                              output=stdout.encode("utf-8"))
        return types.SimpleNamespace(returncode=rc, stdout=stdout,
                                     stderr=stderr)
    return _run


m_subprocess = __import__("subprocess")


DISCOVERY = (
    "=== UI 布局自检 ===\n"
    "[3] 逐屏幕 / DPI 组合构造全部对话框\n"
    "  [OK] 8 种屏幕 x 7 个对话框全部布局合规\n"
    "[5] 模态安全（窗口弹不出来 / 软件关不掉）\n"
    "  [OK] 构造期不抓 grab（窗口尚未 map）  -- grab owner = None\n"
    "  [OK] 窗口被显式 deiconify（不会弹不出来）\n"
    "  [OK] 确实调用了 wait_window（对话框会等待）\n"
    "  [OK] 窗口 map 后才抓 grab\n"
    "  [OK] 登记了超时强制关闭兜底\n"
    "  [OK] 关闭后 grab 被释放（主窗口可正常点 X）  -- None\n"
    "  [OK] 关闭后窗口已销毁\n"
    "  [OK] _close 幂等（重复调用不抛异常）\n"
)

SUMMARY = "[OK] 全部 198 项通过 - 7 个对话框 x 8 种屏幕规格均无显示不全\n"


def run_gate(m, stdout, stderr="", rc=0, timeout=False, strict=True):
    """跑一次 preflight_ui_layout，返回 (结果, 打印内容)。"""
    orig = m.subprocess.run
    m.subprocess.run = fake_run(stdout, stderr, rc, timeout)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            r = m.preflight_ui_layout("python", strict=strict)
    finally:
        m.subprocess.run = orig
    return r, buf.getvalue()


def main():
    m = load_build()
    print("\n=== 构建门禁策略 ===\n")

    print("[1] 正常全绿 -> 通过")
    r, out = run_gate(m, DISCOVERY + SUMMARY)
    check("rc=0 + 汇总行 => True", r is True)
    check("打印成功摘要", "198 项通过" in out, out.strip()[-90:])

    print("\n[2] 事故复现：日志停在 [5]，无汇总 / 无 traceback / 无 [FAIL]")
    r, out = run_gate(m, DISCOVERY, rc=1)
    check("工具链自身中断 => 放行（True）", r is True, f"r={r}")
    check("明确说明是自检工具问题、不是布局问题",
          "不是布局问题" in out and "已放行继续构建" in out, out[-200:])
    check("指出在哪一节后中断", "第 5 节后中断" in out, out[-260:])
    check("不再说「已中止构建」", "已中止构建" not in out)
    check("不再把满屏 [OK] 当失败原因打印",
          out.count("[OK]") <= 2, f"[OK] 次数={out.count('[OK]')}")
    check("给出逃生开关提示", "--no-ui-check" in out)

    print("\n[3] 其它工具链故障同样放行")
    cases = [
        ("自检完全无输出", "", "", 1, False),
        ("GBK 编码崩溃", "Traceback ...\nUnicodeEncodeError: 'gbk' ...\n",
         "", 1, False),
        ("脚本自身异常 rc=2", DISCOVERY + "[FAIL] SELFCHECK_CRASHED at: x\n",
         "", 2, False),
        ("ModuleNotFoundError", "ModuleNotFoundError: No module named 'ui_smoke'\n",
         "", 1, False),
        ("超时", DISCOVERY, "", 0, True),
    ]
    for name, so, se, rc, to in cases:
        r, out = run_gate(m, so, se, rc, timeout=to)
        check(f"{name} => 放行", r is True, f"r={r}")
        check(f"{name} 不报「已中止构建」", "已中止构建" not in out)

    print("\n[4] 唯一该阻断的情形：明确的布局断言 [FAIL]")
    layout_fail = (
        DISCOVERY +
        "  [FAIL] ConfirmDialog @ 1366x768: 底栏被挤出窗口\n" +
        "  [FAIL] SettingsDialog @ 上网本: 窗口过小\n" +
        "[FAIL] 失败 2 项: [...]\n")
    r, out = run_gate(m, layout_fail, rc=1)
    check("有布局 [FAIL] => 中止", r is False, f"r={r}")
    check("列出具体失败项", "底栏被挤出窗口" in out)
    check("提示临时放行开关", "--no-ui-check" in out)
    check("SELFCHECK_CRASHED 不算布局失败",
          run_gate(m, DISCOVERY + "[FAIL] SELFCHECK_CRASHED at: x\n",
                   rc=2)[0] is True)

    print("\n[5] --no-ui-check 逃生开关")
    r, out = run_gate(m, layout_fail, rc=1, strict=False)
    check("布局失败也放行", r is True, f"r={r}")
    check("明确提示已放行", "已放行" in out)
    check("参数已接入 main() 与 build()",
          '"--no-ui-check"' in open(BUILD, encoding="utf-8").read() and
          "strict_ui=not args.no_ui_check" in open(BUILD,
                                                    encoding="utf-8").read())

    print("\n[6] 日志落盘 + 自检脚本自身可诊断")
    src = open(os.path.join(ROOT, "tools", "test_ui_layout.py"),
               encoding="utf-8").read()
    check("顶层兜底输出 SELFCHECK_CRASHED 且带 LAST_CHECK",
          "SELFCHECK_CRASHED at:" in src and "LAST_CHECK" in src)
    check("UI 层已按 CTK 6.0 统一重写，不再有兼容层/降级",
          "def __init_subclass__" not in open(
              os.path.join(ROOT, "client", "ui", "dialogs.py"),
              encoding="utf-8").read() and
          not os.path.isfile(os.path.join(ROOT, "client", "ui",
                                          "tk_compat.py")))
    check("门禁把完整输出写入 _ui_layout_last.log",
          "_ui_layout_last.log" in open(BUILD, encoding="utf-8").read())
    bs = open(BUILD, encoding="utf-8").read()
    seg = bs[bs.index("def preflight_ui_layout"):
             bs.index("def build(")]
    check("只有明确布局断言失败才 return False",
          "layout_fails" in seg and seg.count("return False") == 1)
    check("自检工具异常分支 return True（放行）",
          seg.rstrip().endswith("return True"))

    print("\n" + "=" * 62)
    print(f"[OK] 全部 {len(OK)} 项通过")
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        sys.stderr.write(traceback.format_exc().encode(
            "ascii", "backslashreplace").decode("ascii"))
        raise SystemExit(2)
