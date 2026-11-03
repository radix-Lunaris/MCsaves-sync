# -*- coding: utf-8 -*-
"""
客户端网络策略回归测试：**客户端永远直连，绝不走代理**。

背景：系统里装了 Clash / v2ray 会设置 HTTP_PROXY 等环境变量，
requests 默认 trust_env=True 会把同步流量也丢给本地代理。
曾经一度把客户端默认改成走 10808 代理，结果代理没开就连不上服务器。
现在客户端恒定直连；需要代理的是 push.bat 推 GitHub（git 侧）。

验证方式：在环境变量里真的塞进 10808 代理，再让 Api 发真实请求，
确认流量**没有**经过代理。

用法： python3 tools/test_proxy.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
sys.path.insert(0, CLIENT)

os.environ["MCSYNC_CONFIG_DIR"] = tempfile.mkdtemp(prefix="mcsync-proxy-")

from core.api import Api, DEFAULT_PROXY_URL, proxy_settings  # noqa: E402
from core.config import cfg  # noqa: E402

OK = []
PROXY_HITS = []
TARGET_PORT = 18771
PROXY_PORT = 10808


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


class TargetHandler(BaseHTTPRequestHandler):
    """假装是 MCSync 服务端"""

    def do_GET(self):
        body = json.dumps({"ok": True, "app": "mcsync", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    """极简 HTTP 代理：转发 GET 并计数"""

    def do_GET(self):
        PROXY_HITS.append(self.path)
        try:
            with urlopen(self.path, timeout=10) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *a):
        pass


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv



def main():
    print("\n=== 客户端网络策略测试（必须永远直连） ===\n")

    # 先在系统环境里塞进代理，模拟用户装了 Clash 的机器
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[k] = "http://127.0.0.1:10808"
    print("[0] 已在环境变量中设置 10808 代理（模拟 Clash 常驻）")
    check("默认配置为直连", cfg.get("proxy_mode") == "off", str(cfg.get("proxy_mode")))

    print("\n[1] 决策：任何目标都返回直连")
    for host in ("47.117.101.13", "192.168.1.10", "127.0.0.1"):
        t, px, note = proxy_settings(f"http://{host}:8650")
        check(f"  {host} 直连", t is False and px.get("http", "") == "", note)

    print("\n[2] 真实请求：流量不得经过代理")
    serve(TargetHandler, TARGET_PORT)
    serve(ProxyHandler, PROXY_PORT)
    time.sleep(0.6)

    api = Api(f"http://127.0.0.1:{TARGET_PORT}")
    check("Api 声明直连", "直连" in api.proxy_note, api.proxy_note)
    check("session.trust_env=False", api.session.trust_env is False, "")
    check("session.proxies 为空", api.session.proxies.get("http", "") == "",
          str(api.session.proxies))

    before = len(PROXY_HITS)
    r = api.health()
    after = len(PROXY_HITS)
    check("请求成功", r.get("ok") is True, str(r))
    check("流量未经过代理", after == before, f"代理收到 {after - before} 次（应为 0）")

    print("\n[3] 旧配置残留 custom 会被自动纠正")
    cfg.set("proxy_mode", "custom")
    cfg.set("proxy_url", "http://127.0.0.1:10808")
    cfg.load()
    check("重载后恢复为 off", cfg.get("proxy_mode") == "off", str(cfg.get("proxy_mode")))
    check("残留的 proxy_url 已清除", not cfg.get("proxy_url"), str(cfg.get("proxy_url")))

    print("\n" + "=" * 58)
    bad = [n for n, ok_ in OK if not ok_]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 客户端始终直连")
    print("=" * 58)


if __name__ == "__main__":
    main()
