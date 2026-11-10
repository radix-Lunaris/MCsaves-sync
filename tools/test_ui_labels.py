# -*- coding: utf-8 -*-
"""
「版本标记」与「账号角色显示」回归测试

两个真实反馈：
  1. 上传时一直显示 v1、v2、v3… 递增。vN 是单个存档内部的序号，
     不同实例/不同整合包的存档并排展示时毫无可比性，用户只关心上传时间。
     现在所有面向用户的标识一律改成时间，版本 id 仅用于接口定位。
  2. 管理员账号在客户端显示成「普通用户」。
     根因：self.me 只在 login() 里赋值；启动自动登录（已配对）时
     ensure_login() 直接返回、不刷新身份，self.me 是空字典。
     另外改设置 / 换地址会新建 Api 实例，me 同样丢失。

用法： python3 tools/test_ui_labels.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main():
    print("\n=== 版本标记 / 角色显示 回归测试 ===\n")

    # ---------------- 1. 客户端不再出现 vN ----------------
    print("[1] 客户端界面不得出现 vN 版本标识")
    ui_files = ["client/ui/main_window.py", "client/ui/dialogs.py",
                "client/ui/widgets.py", "client/ui/login_window.py"]
    bad = []
    for rel in ui_files:
        s = src(rel)
        for pat in (r"v\{r\['version'\]", r"v\{vid\}", r"已同步 v", r"v\{v\['id'\]\}"):
            for m in re.finditer(pat, s):
                line = s[:m.start()].count("\n") + 1
                bad.append((rel, line, m.group(0)))
    check("四处界面均无 vN 显示", not bad, str(bad))

    # 注释里提到 v1/v2 是允许的（说明文档），但代码行不能是显示文案
    print("\n[2] 上传/下载结果携带上传时间")
    sync_s = src("client/core/sync.py")
    check("上传结果含 version_time", '"version_time": vtime' in sync_s, "")
    check("下载结果含 version_time", '"version_time": vtime' in sync_s, "")
    check("上传时间取服务端 created_at_str", 'created_at_str' in sync_s, "")
    mw_s = src("client/ui/main_window.py")
    check("上传提示使用 version_time", 'r.get("version_time")' in mw_s, "")
    check("下载提示使用 version_time", mw_s.count('r.get("version_time")') >= 3,
          str(mw_s.count('r.get("version_time")')))

    # ---------------- 2. 版本弹窗以时间为主标识 ----------------
    print("\n[3] 版本管理弹窗以时间为主标识")
    dlg = src("client/ui/dialogs.py")
    check("行首是时钟图标而非 #N / vN", 'text="🕒"' in dlg, "")
    check("主标题显示 created_at_str", 'text=vtime' in dlg, "")
    check("回调携带时间参数", "self.on_action(action, vid, vtime)" in dlg, "")
    check("删除确认用时间描述", "的这次上传" in mw_s, "")
    check("回滚提示用时间描述", "已回滚到" in mw_s, "")

    # ---------------- 3. 本地存档徽章 ----------------
    print("\n[4] 本地存档卡片显示同步时间")
    w = src("client/ui/widgets.py")
    check("徽章用已同步 · 时间", "☁ 已同步 · " in w, "")
    check("不再含 已同步 v", "已同步 v" not in w, "")
    models = src("client/core/models.py")
    check("SaveState 提供 last_sync", "def last_sync" in models, "")
    check("badge() 不含 v 标记", "☁ v" not in models, "")

    # ---------------- 4. Web 管理页 ----------------
    print("\n[5] Web 管理页同样移除 vN")
    web = src("server/static/index.html")
    check("版本行显示 🕒 时间", 'class="vid">🕒 ${fmtTime(v.created_at)}' in web, "")
    check("不再有 v${v.id}", "v${v.id}" not in web, "")
    check("回滚确认用时间", "回滚到 ${label} 的上传" in web, "")
    check("删除确认用时间", "在 ${label} 的这次上传" in web, "")
    check("回滚/删除回调接收时间参数",
          all(x in web for x in ("function(name,vid,vt)", "'${fmtTime(v.created_at)}'")), "")
    # JS 仍可执行（上次事故就是脚本坏了导致页面卡死）
    import shutil
    import subprocess
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", web, re.S)
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        for i, b in enumerate(blocks):
            fp = f"/tmp/mcsync_label_{i}.js"
            with open(fp, "w", encoding="utf-8") as f:
                f.write(b)
            r = subprocess.run([node, "--check", fp], capture_output=True, text=True)
            check(f"脚本块 {i} 语法通过", r.returncode == 0, (r.stderr or "")[:120])

    # ---------------- 5. 角色刷新 ----------------
    print("\n[6] 已配对时必须刷新角色（修复管理员显示成普通用户）")
    api_s = src("client/core/api.py")
    check("定义了 refresh_me", "def refresh_me" in api_s, "")
    check("refresh_me 走 /api/me", '"/api/me"' in api_s, "")
    check("ensure_login 已配对分支调用 refresh_me",
          re.search(r"if self\.client_secret[\s\S]{0,300}?self\.refresh_me\(\)", api_s) is not None, "")

    # 真跑一次：模拟"已配对 + 服务端是管理员"
    import core.config as config_mod
    from core.api import Api
    old_dir, old_data = config_mod.cfg.dir, dict(config_mod.cfg.data)
    try:
        import tempfile
        config_mod.cfg.dir = tempfile.mkdtemp(prefix="mcsync_role_")
        config_mod.cfg.set("server_url", "http://127.0.0.1:1")
        config_mod.cfg.set("username", "boss")
        config_mod.cfg.set_secret("Passw0rd123", "password")
        config_mod.cfg.set("client_id", "cid-test")
        config_mod.cfg.set_secret("s" * 64)
        config_mod.cfg.set("paired_server", "http://127.0.0.1:1")

        api = Api("http://127.0.0.1:1")
        calls = []

        def fake_json(method, path, **kw):
            calls.append((method, path))
            return {"user": {"username": "boss", "display": "Boss",
                             "role": "admin", "disabled": False}}

        api._json = fake_json
        did = api.ensure_login()
        check("已配对时不重复登录", did is False, str(did))
        check("确实请求了 /api/me", ("GET", "/api/me") in calls, str(calls))
        check("self.me 拿到 admin 角色", api.me.get("role") == "admin", str(api.me))
        check("显示名一并取回", api.me.get("display") == "Boss", str(api.me))
    finally:
        config_mod.cfg.dir = old_dir
        config_mod.cfg.data = old_data

    # 界面侧：角色文案必须按 me 里的 role 判定
    print("\n[7] 顶栏角色判定取自 me.role")
    for rel, needle in (("client/ui/main_window.py", 'who.get("role") == "admin"'),
                        ("client/ui/dialogs.py", 'who.get("role") == "admin"')):
        check(f"{os.path.basename(rel)} 按 admin 判定", needle in src(rel), "")

    # ---------------- 6. 防复发：AST 检查 ----------------
    print("\n[8] 语法完整性")
    for rel in ("client/core/api.py", "client/core/sync.py", "client/core/models.py",
                "client/ui/main_window.py", "client/ui/dialogs.py",
                "client/ui/widgets.py", "server/app.py"):
        try:
            ast.parse(src(rel))
            check(f"{os.path.basename(rel)} 语法通过", True, "")
        except SyntaxError as e:
            check(f"{os.path.basename(rel)} 语法通过", False, str(e))

    print("\n" + "=" * 58)
    bad_items = [n for n, o in OK if not o]
    if bad_items:
        print(f"❌ 失败 {len(bad_items)} 项: {bad_items}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 版本标识改为时间、角色显示正确")
    print("=" * 58)


if __name__ == "__main__":
    main()
