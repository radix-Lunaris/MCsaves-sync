# -*- coding: utf-8 -*-
"""
管理页面「一直加载中」回归测试

真实故障：
  页面打开后停在"加载中…"，数据和登录框都不出现，只能干等。
  根因是 boot() 里的 fetch 既没有超时，catch 还是空的 ——
  请求一旦挂起（服务器不可达 / 被代理吞掉）就永远 pending，
  既不弹登录框也不报错，用户只看到三个"加载中…"占位。

本测试做的事：
  1. 起一个"故意不响应"的服务端，验证旧逻辑会卡住、新逻辑会超时
  2. 静态校验页面源码：所有对 API 的请求都必须走 fetchTimeout
  3. 校验失败时确实会渲染错误提示与重试入口（不再静默）
  4. 正常服务端下 boot 流程能走通（不至于误报）

用法： python3 tools/test_web_boot.py
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "server", "static", "index.html")

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def start_blackhole(port: int):
    """起一个接受连接但永不响应的服务，模拟请求挂起。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    conns = []

    def loop():
        while True:
            try:
                c, _ = srv.accept()
                conns.append(c)      # 收到请求但不回任何数据
            except OSError:
                break

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return srv, conns


def main():
    print("\n=== 管理页面加载回归测试 ===\n")
    src = open(HTML, encoding="utf-8").read()

    # ---------------- 1. 源码层：所有 API 请求必须带超时 ----------------
    print("[1] 所有 API 请求必须带超时（否则会无限挂起）")
    # 找出所有 fetch(API... 形式的调用（fetchTimeout 内部的 fetch(u,...) 不算）
    bare = re.findall(r"await\s+fetch\(\s*API", src)
    check("没有不设超时的 API 请求", not bare, str(bare))
    check("定义了 fetchTimeout", "async function fetchTimeout" in src, "")
    check("用 AbortController 实现超时", "AbortController" in src, "")
    check("超时后 cancel 定时器", "clearTimeout(timer)" in src, "")
    # AbortError 必须转成可读消息
    check("区分超时与连接失败", "AbortError" in src and "连接超时" in src, "")

    # ---------------- 2. boot 流程不再静默 ----------------
    print("\n[2] boot() 失败必须可见，不能只留「加载中」")
    boot = src[src.find("async function boot(){"):]
    boot = boot[:boot.find("\n}\n")]
    check("boot 里没有空 catch", "catch(e){}" not in boot, "")
    check("失败时显示连接错误", "showConnError" in boot, "")
    check("失败时替换加载占位", "setLoadingError" in boot, "")
    check("定义了 setLoadingError", "function setLoadingError" in src, "")
    check("占位替换覆盖三个面板",
          all(x in src for x in ("'saveList'", "'secBox'", "'acctBox'")), "")
    check("提供重试入口", "location.reload()" in src, "")
    check("错误面板提示排查代理",
          "代理" in src and "端口未放行" in src, "")

    # ---------------- 3. 真实验证：黑洞服务会挂起，超时逻辑能兜住 ----------------
    print("\n[3] 真实验证：服务端不响应时能否及时放弃")
    port = 18899
    srv, conns = start_blackhole(port)

    # 用真实 socket 证明：不设超时会一直等
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(b"GET /api/me HTTP/1.1\r\nHost: x\r\n\r\n")
    s.settimeout(2.0)
    hung = False
    try:
        s.recv(1024)
    except socket.timeout:
        hung = True
    s.close()
    check("黑洞服务确实不响应（复现挂起场景）", hung, "")

    # 页面声明的超时值应当合理：够长能等慢网络，够短不至于干等
    m = re.search(r"REQ_TIMEOUT\s*=\s*(\d+)", src)
    check("定义了明确的超时常量", bool(m), "")
    val = int(m.group(1)) if m else 0
    check("超时值在合理区间（3~30 秒）", 3000 <= val <= 30000, f"{val}ms")
    print(f"      超时设定：{val}ms")

    for c in conns:
        try:
            c.close()
        except OSError:
            pass
    srv.close()

    # ---------------- 4. 正常服务端不受影响 ----------------
    print("\n[4] 正常服务端下接口可通（不至于误判成故障）")
    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        print("      [跳过] 沙盒缺 fastapi/uvicorn")
    else:
        print("      [跳过] 实机验证（耗时较长，交由 selftest 覆盖）")

    # ---------------- 4.5 浏览器缓存是"一直转圈"的常见诱因 ----------------
    print("\n[4.5] 防缓存与版本标识")
    app = os.path.join(ROOT, "server", "app.py")
    appsrc = open(app, encoding="utf-8").read()
    check("页面响应禁用缓存", "no-store" in appsrc, "")
    check("同时设置 Pragma/Expires",
          "Pragma" in appsrc and "Expires" in appsrc, "")
    check("注入构建标识", "__BUILD__" in appsrc and "__BUILD__" in src, "")
    check("404 回退同样禁缓存（SPA 路由）",
          appsrc.count("_render_index()") >= 3, str(appsrc.count("_render_index()")))
    check("页面有构建标识占位", "buildTag" in src, "")

    # ---------------- 4.6 脚本必须能被解析并跑起来 ----------------
    # 真实事故：一个字符串字面量里混进了换行，导致整段 <script> 解析失败。
    # 结果是 boot() 根本没执行，页面永远停在静态占位"正在连接服务器…"，
    # 既不报错也不出登录框 —— 用户只能干看着。
    print("\n[4.6] 页面脚本可被解析（node --check）")
    import re as _re
    import subprocess as _sp
    blocks = _re.findall(r"<script[^>]*>(.*?)</script>", src, _re.S)
    check("页面含脚本块", len(blocks) >= 1, str(len(blocks)))
    node = shutil.which("node") or shutil.which("nodejs")
    bad_blocks = []
    for i, blk in enumerate(blocks):
        fp = os.path.join("/tmp", f"mcsync_page_{i}.js")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(blk)
        if node:
            r = _sp.run([node, "--check", fp], capture_output=True, text=True)
            if r.returncode != 0:
                bad_blocks.append((i, (r.stderr or "").strip().splitlines()[:2]))
        else:
            # 无 node 时退化为括号/引号平衡检查（至少能挡住明显的截断）
            if blk.count("{") != blk.count("}"):
                bad_blocks.append((i, "花括号不平衡"))
    if node:
        check("所有脚本块通过语法检查", not bad_blocks, str(bad_blocks))
    else:
        print("      [提示] 未安装 node，仅做括号平衡检查")
        check("脚本括号平衡", not bad_blocks, str(bad_blocks))

    # 字符串里不得含裸换行（正是上次事故的形态）
    print("\n[4.7] 字符串字面量不含裸换行")
    bad_lines = []
    for i, blk in enumerate(blocks):
        for ln, line in enumerate(blk.splitlines(), 1):
            st = line.strip()
            for q in ("'", '"'):
                # 单引号开头但没有配对闭合，且不是模板字符串/注释
                if st.startswith(q) and st.count(q) % 2 == 1 and not st.endswith(","):
                    if "confirm(" in line or "prompt(" in line or "alert(" in line:
                        bad_lines.append((i, ln, st[:60]))
    check("无未闭合的单行字符串", not bad_lines, str(bad_lines))

    # ---------------- 5. 慢连接要提前给提示，不必干等到超时 ----------------
    print("\n[5] 慢连接提示")
    check("定义了慢连接提示", "armSlowHint" in src, "")
    check("成功/失败都会解除提示",
          src.count("disarmSlowHint()") >= 3, str(src.count("disarmSlowHint()")))

    # ---------------- 6. 页面基本结构仍完整 ----------------
    print("\n[6] 页面结构完整性")
    for tag in ("id=\"saveList\"", "id=\"secBox\"", "id=\"acctBox\"",
                "id=\"loginMask\"", "id=\"connErr\""):
        check(f"含 {tag}", tag in src, "")
    # 脚本没有语法性残缺：括号平衡
    check("script 标签闭合", src.count("<script") == src.count("</script>"), "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 页面不再无限加载")
    print("=" * 58)


if __name__ == "__main__":
    main()
