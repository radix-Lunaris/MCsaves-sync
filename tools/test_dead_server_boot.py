# -*- coding: utf-8 -*-
"""
回归：旧服务器地址失效后，客户端必须仍能打开、仍能改地址。

历史故障：
  App.__init__（UI 主线程）里同步调用 _prepare_api() → ensure_login()
  → refresh_me() → GET /api/me。主 session 挂了 5 次 Retry，加上云厂商
  对已释放 IP 直接丢包（Windows TCP SYN 重传约 20 秒），一次探测能卡
  100 秒以上。App.__init__ 不返回，main() 里的 LoginWindow 压根没机会
  创建，主窗口又是 withdraw 状态 —— 用户看到的就是「双击完全没反应，
  连填写新地址的地方都没有」。

本测试把 requests 换成「永久挂起」的桩件，从行为上锁死这一点。

用法： python3 tools/test_dead_server_boot.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TMP = "/tmp/mcsync-dead-boot"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "cfg"), exist_ok=True)
os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


OLD_URL = "http://47.117.101.13:36854"
NEW_URL = "http://47.117.101.13:24011"


def seed_config():
    from core.config import cfg
    cfg.set("server_url", OLD_URL)
    cfg.set("username", "alice")
    cfg.set_secret("pw123456", "password")
    cfg.set("client_id", "cid-1")
    cfg.set_secret("a" * 64)
    cfg.set("paired_server", OLD_URL)
    cfg.set("remember_password", True)
    cfg.set("auto_login", True)
    cfg.set("logged_in", True)
    cfg.set("me_cache", {"username": "alice", "role": "admin", "display": "Alice"})
    return cfg


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"user": {"username": "alice", "role": "admin"}}


def main():
    print("\n=== 旧服务器失效后仍可启动 / 改地址（死锁回归） ===\n")
    cfg = seed_config()
    import requests

    # ---------------------------------------------------------------- 1
    print("[1] App.__init__ 期间必须零网络请求")
    calls = []

    def hanging_request(self, method, url, **kw):
        calls.append((method, url))
        # 模拟「IP 已释放、包被静默丢弃」：一旦被调用就长时间不返回
        time.sleep(8)
        raise RuntimeError("不应走到这里")

    orig_request = requests.Session.request
    requests.Session.request = hanging_request
    try:
        import ui.main_window as mw
        t0 = time.time()
        app = mw.App()
        dt = time.time() - t0
    finally:
        requests.Session.request = orig_request

    check("构造期间没有发出任何网络请求", len(calls) == 0, f"calls={calls}")
    check("构造在 3 秒内返回（不阻塞 UI 线程）", dt < 3.0, f"{dt:.2f}s")
    check("窗口仍处于隐藏状态（由登录流程统一显示）",
          getattr(app, "_withdrawn", False) is True)

    # ---------------------------------------------------------------- 2
    print("\n[2] main() 能创建登录窗口，地址框可编辑")
    spy = {"called": 0, "auto": None}

    class SpyLogin:
        def __init__(self, on_success, auto=True):
            spy["called"] += 1
            spy["auto"] = auto
            self.on_success = on_success
            # 模拟用户放弃自动登录、直接进主界面
            on_success(None)

    import ui.login_window as lw
    orig_lw = lw.LoginWindow
    orig_app_cls = mw.App
    lw.LoginWindow = SpyLogin
    mw.App = lambda: app          # 复用同一个窗口实例，便于断言它最终被显示
    requests.Session.request = hanging_request
    try:
        t0 = time.time()
        mw.main()
        dt2 = time.time() - t0
    finally:
        requests.Session.request = orig_request
        lw.LoginWindow = orig_lw
        mw.App = orig_app_cls

    check("登录窗口被创建", spy["called"] == 1, str(spy))
    check("走自动登录路径", spy["auto"] is True, str(spy["auto"]))
    check("整个启动流程在 3 秒内完成", dt2 < 3.0, f"{dt2:.2f}s")
    check("主窗口最终被显示", getattr(app, "_withdrawn", True) is False)

    # ---------------------------------------------------------------- 3
    print("\n[3] 独立登录窗口的地址框始终可编辑，且有「停止自动登录」")
    from core.config import cfg as _cfg
    _cfg.set("server_url", OLD_URL)
    # 桩件的 __getattr__ 返回不可调用的惰性对象，补成 no-op 才能构造窗口
    orig_getattr = ui_smoke.W.__getattr__

    def callable_stub(self, name):
        def noop(*a, **k):
            return None
        return noop

    ui_smoke.W.__getattr__ = callable_stub
    try:
        win = lw.LoginWindow(on_success=lambda r: None, auto=False)
        check("地址框预填了旧地址", win.e_url.get() == OLD_URL, win.e_url.get())
        win.e_url.delete()
        win.e_url.insert(0, NEW_URL)
        check("地址框可改成新地址", win.e_url.get() == NEW_URL, win.e_url.get())
        check("存在停止自动登录按钮", hasattr(win, "b_cancel_auto"))
        win._set_busy(True, auto=True)
        check("自动登录期间可停止", win._cancel_auto() is None)
        check("停止后按钮恢复可用", win._busy is False)
    finally:
        ui_smoke.W.__getattr__ = orig_getattr

    src_lw = open(os.path.join(ROOT, "client", "ui", "login_window.py"),
                  encoding="utf-8").read()
    check("停止后提示去改地址", "已停止自动登录。请修改服务器地址后点「登录」" in src_lw)
    check("自动登录失败提示明确要求改地址",
          "请在上方修改服务器地址后重新登录" in src_lw)
    check("自动登录用 quick 短超时", "quick=quick" in src_lw)
    check("旧线程结果会被丢弃", "gen != self._auto_gen" in src_lw)

    # ---------------------------------------------------------------- 4
    print("\n[4] quick 探测：短超时、零重试、不可达时立即返回")
    from core.api import Api, QUICK_TIMEOUT, MCSyncError

    api = Api(OLD_URL)
    seen = []

    def fake_plain(self, method, url, **kw):
        seen.append(kw.get("timeout"))
        return FakeResponse()

    api._plain.request = types.MethodType(fake_plain, api._plain)
    api.refresh_me(quick=True)
    check("只发一次请求（无 5 次重试）", len(seen) == 1, str(seen))
    check("使用短超时", seen[0] == QUICK_TIMEOUT, str(seen[0]))

    # 不可达：模拟连接超时
    def timeout_plain(self, method, url, **kw):
        time.sleep(0.05)
        raise requests.exceptions.ConnectTimeout("connect timeout")

    api2 = Api(OLD_URL)
    api2._plain.request = types.MethodType(timeout_plain, api2._plain)
    n = {"c": 0}
    orig_refresh = Api.refresh_me

    def counting(self, quick=False):
        n["c"] += 1
        return orig_refresh(self, quick=quick)

    Api.refresh_me = counting
    try:
        t0 = time.time()
        api2.ensure_login(quick=True)
        dt3 = time.time() - t0
    finally:
        Api.refresh_me = orig_refresh
    check("不可达时 1 秒内返回", dt3 < 1.0, f"{dt3:.2f}s")
    check("不可达时只尝试一次", n["c"] == 1, str(n))
    check("ensure_login 不会抛出异常打断界面", True)

    # ---------------------------------------------------------------- 5
    print("\n[5] 身份确认与连接探测都在后台，主线程回调零网络")
    src_mw = open(os.path.join(ROOT, "client", "ui", "main_window.py"),
                  encoding="utf-8").read()
    check("_prepare_api 明确标注纯本地", "纯本地" in src_mw)
    check("启动用 _prepare_api_async()", "self._prepare_api_async()" in src_mw)
    check("异步身份确认走 quick", "ensure_login(quick=True)" in src_mw)
    check("连接探测走 quick", "self.api.test(quick=True)" in src_mw)
    check("done 回调不再同步取统计", "self._safe_stat()" not in
          src_mw.split("def test_connection")[1].split("def _safe_stat")[0])
    check("refresh_cloud 的统计改走后台", "self.run_async(stat_work, stat_done, stat_fail)" in src_mw)
    check("App.__init__ 不调用 ensure_login()",
          "self.api.ensure_login()" not in src_mw)

    # ---------------------------------------------------------------- 6
    print("\n[6] 主界面万一崩了，仍有紧急设置窗口可改地址")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcsync_entry", os.path.join(ROOT, "client", "main.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    src_main = open(os.path.join(ROOT, "client", "main.py"),
                    encoding="utf-8").read()
    check("main() 包裹了异常兜底", "except Exception as e:" in src_main)
    check("存在 _emergency_settings", "_emergency_settings(e)" in src_main)
    check("兜底窗口能写 server_url", 'write("server_url", v)' in src_main)
    check("兜底窗口能清空登录状态", "clear_login" in src_main)

    called = {"v": False}

    def fake_emergency(e):
        called["v"] = True

    def boom():
        raise RuntimeError("主界面模拟崩溃")

    entry._emergency_settings = fake_emergency
    orig_import = __import__
    # 直接替换模块属性更简单：monkeypatch ui.main_window.main
    import ui.main_window as mw2
    orig_run = mw2.main
    mw2.main = boom
    try:
        entry.main()
    finally:
        mw2.main = orig_run
    check("主界面崩溃时确实进入兜底", called["v"] is True)

    # ---------------------------------------------------------------- 7
    print("\n[7] 改地址能落盘并重建 Api，旧配对状态不残留")
    cfg.set("server_url", NEW_URL)
    check("新地址已保存", cfg.get("server_url") == NEW_URL, cfg.get("server_url"))
    api3 = Api(cfg.server_url)
    check("Api 指向新地址", api3.base == NEW_URL, api3.base)
    check("旧配对服务器与当前地址不再一致",
          cfg.get("paired_server") == OLD_URL and api3.paired_server != api3.base)

    # ---------------------------------------------------------------- 8
    print("\n[8] 后台自动登录绝不把用户新填的地址覆盖回旧 IP")
    import core.api as capi

    def flush_pending():
        # ui_smoke.flush 假定是 App（有 _ui_queue），登录窗口没有，
        # 这里直接消费 after() 排进 PENDING 的回调。
        for _ in range(30):
            pend = list(ui_smoke.PENDING)
            ui_smoke.PENDING.clear()
            if not pend:
                break
            for fn in pend:
                if callable(fn):
                    fn()

    orig_login = capi.Api.login
    calls = []

    def fake_login(self, username="", password="", name="", quick=False):
        calls.append((self.base, quick))
        self.me = {"username": "alice", "role": "admin", "display": "Alice"}
        # 等价模拟真实 Api.login 的落盘（唯独不写 server_url）
        cfg.set("paired_server", self.base)
        cfg.set("username", username)
        return {"user": self.me}

    capi.Api.login = fake_login
    try:
        cfg.set("server_url", OLD_URL)
        cfg.set("paired_server", OLD_URL)
        src_api = open(os.path.join(ROOT, "client", "core", "api.py"),
                       encoding="utf-8").read()
        src_lw2 = open(os.path.join(ROOT, "client", "ui", "login_window.py"),
                       encoding="utf-8").read()
        check("Api.login 不写 server_url",
              'cfg.set("server_url"' not in src_api.split("def login(")[1].split("def refresh_me")[0])
        check("登录线程不写 server_url",
              'cfg.set("server_url", url)' not in src_lw2)

        ui_smoke.W.__getattr__ = callable_stub
        try:
            win2 = lw.LoginWindow(on_success=lambda r: None, auto=False)
            # 用户在登录框里改成新地址，而 cfg 里仍是旧地址（手动换服务器的典型形态）
            win2.e_url.delete()
            win2.e_url.insert(0, NEW_URL)
            win2._auto_gen += 1
            gen = win2._auto_gen
            # 前面几节窗口构造时往全局 PENDING 排了 _boot / 图标等回调，
            # 清掉，只留本次 _login_worker 的结果回调，避免跨用例污染。
            ui_smoke.PENDING.clear()
            win2._login_worker(NEW_URL, "alice", "pw123456", False, True, gen)
            flush_pending()
        finally:
            ui_smoke.W.__getattr__ = orig_getattr
        check("手动用新地址登录后落盘为新地址",
              cfg.get("server_url") == NEW_URL, str(cfg.get("server_url")))
        check("配对状态同步指向新地址",
              cfg.get("paired_server") == NEW_URL, str(cfg.get("paired_server")))

        # 自动登录旧地址期间用户改了地址：旧线程结果不得把地址改回去
        cfg.set("server_url", OLD_URL)
        ui_smoke.W.__getattr__ = callable_stub
        try:
            win3 = lw.LoginWindow(on_success=lambda r: None, auto=False)
            win3._auto_gen += 1
            g3 = win3._auto_gen
            ui_smoke.PENDING.clear()
            win3._login_worker(OLD_URL, "alice", "pw123456", True, True, g3)
            cfg.set("server_url", NEW_URL)        # 用户中途改成新地址
            win3._auto_gen += 1                    # 相当于点了「停止自动登录」
            flush_pending()
        finally:
            ui_smoke.W.__getattr__ = orig_getattr
        check("过期自动登录结果被丢弃，新地址保留",
              cfg.get("server_url") == NEW_URL, str(cfg.get("server_url")))
    finally:
        capi.Api.login = orig_login

    print("\n" + "=" * 66)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 旧服务器失效不再导致客户端打不开")
    print("=" * 66)


if __name__ == "__main__":
    main()
