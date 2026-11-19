# -*- coding: utf-8 -*-
"""构建门禁自身的编码安全回归（全 ASCII 输出，任何代码页都能跑）。

事故：Windows 中文控制台下 build.bat 显示

    [X] UI 布局自检未通过，已中止构建：
    （中间一片空白，没有任何细节）

实际**不是布局坏了**：build.py 用 capture_output 起子进程后，Windows
Python 把管道编码切成 GBK/CP936，tools/test_ui_layout.py 第一个
`print("✅ ...")` 就抛 UnicodeEncodeError 崩溃，有效输出几乎为空，
rc=1。门禁只看到「未通过」，却打不出任何细节。

本文件用子进程 + 多种 PYTHONIOENCODING 真正复现 / 验证，避免只在
Linux UTF-8 沙盒里自测通过、到用户机器上又炸。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "tools", "test_ui_layout.py")
BUILD = os.path.join(ROOT, "client", "build.py")
TMP = os.path.join(tempfile.gettempdir(), "mcsync-ui-layout")

OK = []


def check(name, cond, extra=""):
    OK.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name
          + ("  -- " + str(extra) if extra else ""))
    if not cond:
        raise AssertionError(name + (" | " + str(extra) if extra else ""))


def src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def run_checker(enc):
    """在指定 PYTHONIOENCODING 下跑一次自检，返回 (rc, stdout, stderr)。"""
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = enc
    env["PYTHONUTF8"] = "0"          # 刻意关掉自动 UTF-8，模拟最苛刻场景
    env["MCSYNC_CONFIG_DIR"] = TMP
    try:
        p = subprocess.run([sys.executable, "-u", CHECKER],
                           cwd=ROOT, capture_output=True, timeout=240,
                           env=env)
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return (-99, "", "TIMEOUT")
    except Exception as e:
        return (-98, "", repr(e))


def main():
    print("\n=== 构建门禁编码安全 ===\n")

    print("[1] 复现事故：不带兼容层的旧写法在 GBK 下必然崩")
    # 直接在一个临时脚本里打印 emoji，验证「管道 + GBK」确实会抛
    probe = os.path.join(tempfile.gettempdir(), "_mcsync_enc_probe.py")
    with open(probe, "w", encoding="ascii") as f:
        f.write("print('\\u2705 layout ok')\n")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "gbk"
    env["PYTHONUTF8"] = "0"
    p = subprocess.run([sys.executable, "-u", probe], capture_output=True,
                       env=env, timeout=30)
    err = (p.stderr or b"").decode("utf-8", "replace")
    check("GBK 管道下打印 emoji 确实抛 UnicodeEncodeError",
          p.returncode != 0 and "UnicodeEncodeError" in err,
          err.strip().splitlines()[-1] if err.strip() else "no stderr")
    try:
        os.remove(probe)
    except OSError:
        pass

    print("\n[2] 修复后：四种编码下自检都必须完整跑完")
    for enc in ("utf-8", "gbk", "ascii", "cp936"):
        rc, out, err = run_checker(enc)
        check(f"PYTHONIOENCODING={enc} 自检 rc=0", rc == 0,
              f"rc={rc} stderr={err[:120]!r}")
        check(f"{enc} 下输出含成功摘要",
              "全部" in out and "项通过" in out,
              out.strip().splitlines()[-2] if out.strip() else "no stdout")
        check(f"{enc} 下没有 UnicodeEncodeError",
              "UnicodeEncodeError" not in out and
              "UnicodeEncodeError" not in err)
        check(f"{enc} 下没有自检自身崩溃",
              "SELFCHECK CRASHED" not in out and "Traceback" not in err)

    print("\n[3] 自检脚本源码护栏")
    ts = src("tools/test_ui_layout.py")
    check("存在 _safe_stdio 且在 import 期调用",
          "def _safe_stdio" in ts and "_safe_stdio()" in ts and
          ts.find("_safe_stdio()") < ts.find("import ui_smoke"))
    check("stdout/stderr 都重配置为 UTF-8 + errors=replace",
          'reconfigure(encoding="utf-8", errors="replace")' in ts and
          '"stdout", "stderr"' in ts)
    check("check() 使用纯 ASCII 标记 [OK]/[FAIL]",
          '"  [OK] " if cond else "  [FAIL] "' in ts)
    check("汇总行不再打印 emoji",
          'print(f"[OK] 全部' in ts and 'print(f"[FAIL] 失败' in ts)
    check("顶层有 except BaseException 兜底并写 ASCII-safe traceback",
          "except BaseException" in ts and "backslashreplace" in ts)
    check("成功摘要保留「全部 N 项通过」供 build.py 抓取",
          "全部 {len(OK)} 项通过" in ts)

    print("\n[4] build.py 子进程环境")
    bs = src("client/build.py")
    seg = bs[bs.index("def preflight_ui_layout"):
             bs.index("def build(")]
    check("注入 PYTHONIOENCODING=utf-8",
          'env["PYTHONIOENCODING"] = "utf-8"' in seg)
    check("注入 PYTHONUTF8=1", 'env["PYTHONUTF8"] = "1"' in seg)
    check("子进程用 -u 无缓冲", '"-u", checker' in seg)
    check("按 UTF-8 解码子进程输出（与注入一致）",
          'encoding="utf-8", errors="replace", env=env' in seg)
    check("失败时区分自检自身崩溃与布局不合规",
          "self_crash" in seg and "不是布局问题" in seg)
    check("失败时打印 [FAIL] 行而非只打尾部输出",
          '"[FAIL]" in l' in seg)
    check("失败时把完整输出落盘便于排查",
          "_ui_layout_last.log" in seg)
    check("超时单独处理", "subprocess.TimeoutExpired" in seg)

    print("\n[5] 反向验证：去掉 _safe_stdio 后 gbk 必须再次失败")
    # 不改真实文件：复制一份到 tools/（必须同目录，否则脚本按 __file__
    # 算出的 ROOT 不对、会先报 ModuleNotFoundError 而不是编码错误），
    # 删掉兼容层 + 换回 emoji，再跑。finally 保证一定清理。
    variant = os.path.join(ROOT, "tools", "_mcsync_layout_reverse_tmp.py")
    old = ts.replace("_safe_stdio()\n", "pass  # removed for reverse test\n", 1)
    old = old.replace('print(("  [OK] " if cond else "  [FAIL] ") + name',
                      'print(("  \\u2705 " if cond else "  \\u274c ") + name', 1)
    try:
        with open(variant, "w", encoding="utf-8") as f:
            f.write(old)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "gbk"
        env["PYTHONUTF8"] = "0"
        env["MCSYNC_CONFIG_DIR"] = TMP
        p = subprocess.run([sys.executable, "-u", variant], cwd=ROOT,
                           capture_output=True, timeout=120, env=env)
        e2 = (p.stderr or b"").decode("utf-8", "replace")
        o2 = (p.stdout or b"").decode("utf-8", "replace")
        check("旧写法在 gbk 下确实 rc!=0 且报 UnicodeEncodeError",
              p.returncode != 0 and
              ("UnicodeEncodeError" in e2 or "\\u2705" in e2),
              f"rc={p.returncode} stderr_tail={e2.strip()[-110:]!r}")
        check("旧写法确实没跑完（无成功摘要）",
              "全部" not in o2 or "项通过" not in o2,
              o2.strip().splitlines()[-1] if o2.strip() else "no stdout")
    finally:
        try:
            os.remove(variant)
        except OSError:
            pass

    print("\n" + "=" * 62)
    print(f"[OK] 全部 {len(OK)} 项通过")
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        sys.stderr.write(
            traceback.format_exc().encode("ascii", "backslashreplace")
            .decode("ascii"))
        raise SystemExit(2)
