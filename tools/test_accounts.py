# -*- coding: utf-8 -*-
"""
账号与权限回归测试

覆盖：
  1) 管理员密钥引导创建首个管理员
  2) 账号密码登录（客户端拿私钥、面板拿会话）
  3) 两级权限：普通用户能同步，不能删存档 / 管账号
  4) 管理员可建号、改角色、停用、删号；最后一个管理员不可被降级或删除
  5) 停用账号立即失效

用法： python3 tools/test_accounts.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, CLIENT)

PORT = int(os.environ.get("TEST_PORT", "8925"))
TMP = os.environ.get("MCSYNC_TEST_TMP", "/tmp/mcsync-accounts")
DATA = os.path.join(TMP, "serverdata")
ADMIN_KEY = "AdminKey123456789"
ADMIN = ("admin", "AdminPass123")
USER = ("alice", "AlicePass123")

import json  # noqa: E402

import requests  # noqa: E402

OK = []
BASE = ""


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def start_server():
    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
               MCSYNC_AUTH="1", MCSYNC_TOKEN=ADMIN_KEY, PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def wait_up():
    for _ in range(60):
        try:
            if requests.get(f"{BASE}/api/health", timeout=3).json().get("ok"):
                return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("服务端启动失败")


def main():
    global BASE
    BASE = f"http://127.0.0.1:{PORT}"
    print("\n=== MCSync 账号与权限回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)

    srv = start_server()
    wait_up()
    check("服务端启动", True)

    try:
        # ---------------------------------------------------- 1. 引导
        print("\n[1] 管理员密钥引导")
        r = requests.get(f"{BASE}/api/bootstrap/status", timeout=5).json()
        check("初始状态为未初始化", r.get("initialized") is False, str(r))

        r = requests.post(f"{BASE}/api/bootstrap", timeout=5, json={
            "admin_key": "wrong-key", "username": "x", "password": "yyyyyy"})
        check("错误的密钥被拒绝", r.status_code in (401, 403), f"HTTP {r.status_code}")

        r = requests.post(f"{BASE}/api/bootstrap", timeout=5, json={
            "admin_key": ADMIN_KEY, "username": ADMIN[0], "password": ADMIN[1],
            "display": "管理员"})
        check("用正确密钥创建管理员成功", r.status_code == 200, r.text[:120])
        check("返回的角色是 admin",
              (r.json().get("user") or {}).get("role") == "admin", r.text[:120])

        r = requests.post(f"{BASE}/api/bootstrap", timeout=5, json={
            "admin_key": ADMIN_KEY, "username": "hacker", "password": "zzzzzz"})
        check("重复引导被拒绝（账号已存在）", r.status_code >= 400, f"HTTP {r.status_code}")

        r = requests.get(f"{BASE}/api/bootstrap/status", timeout=5).json()
        check("状态变为已初始化", r.get("initialized") is True, str(r))

        # ---------------------------------------------------- 2. 面板登录
        print("\n[2] 面板登录（会话 Cookie）")
        s = requests.Session()
        r = s.post(f"{BASE}/api/web/login", timeout=5,
                   json={"username": ADMIN[0], "password": "wrong"})
        check("错误口令登录失败", r.status_code == 401, f"HTTP {r.status_code}")
        r = s.post(f"{BASE}/api/web/login", timeout=5,
                   json={"username": ADMIN[0], "password": ADMIN[1]})
        check("正确口令登录成功", r.status_code == 200, r.text[:120])
        check("已下发会话 Cookie", "mcsync_session" in s.cookies, str(list(s.cookies.keys())))
        me = s.get(f"{BASE}/api/me", timeout=5).json()
        check("/api/me 返回管理员身份",
              (me.get("user") or {}).get("role") == "admin", str(me))

        # ---------------------------------------------------- 3. 建号
        print("\n[3] 管理员创建普通账号")
        r = s.post(f"{BASE}/api/admin/accounts", timeout=5, json={
            "username": USER[0], "password": USER[1], "role": "user", "display": "爱丽丝"})
        check("创建账号成功", r.status_code == 200, r.text[:120])
        r = s.post(f"{BASE}/api/admin/accounts", timeout=5, json={
            "username": USER[0], "password": USER[1], "role": "user"})
        check("重名账号被拒绝", r.status_code >= 400, f"HTTP {r.status_code}")
        lst = s.get(f"{BASE}/api/admin/accounts", timeout=5).json()
        names = [a["username"] for a in lst.get("accounts", [])]
        check("账号列表含两个账号",
              ADMIN[0] in names and USER[0] in names, str(names))

        # ---------------------------------------------------- 4. 客户端登录
        print("\n[4] 客户端登录（账号 → 私钥登记）")
        sec = "a" * 64
        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": USER[1],
            "client_id": "pc-alice", "secret": sec, "name": "AlicePC"})
        check("客户端登录成功", r.status_code == 200, r.text[:150])
        check("返回普通用户角色",
              (r.json().get("user") or {}).get("role") == "user", r.text[:150])

        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": "wrong",
            "client_id": "pc-x", "secret": sec})
        check("客户端错误口令被拒", r.status_code == 401, f"HTTP {r.status_code}")

        # ---------------------------------------------------- 5. 权限
        print("\n[5] 两级权限")

        def signed(cid, secret, method, path, body=b""):
            import hashlib, hmac as _h, secrets as _s
            ts = str(int(time.time()))
            nonce = _s.token_hex(8)
            msg = "\n".join([ts, nonce, method.upper(), path,
                             hashlib.sha256(body).hexdigest()])
            sig = _h.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            return {"X-MCSync-Key": cid, "X-MCSync-Ts": ts,
                    "X-MCSync-Nonce": nonce, "X-MCSync-Sign": sig}

        # 普通用户做一次签名请求：应能正常通过鉴权（同步功能对普通用户开放）
        path = "/api/saves"
        r = requests.get(f"{BASE}{path}", timeout=5,
                         headers=signed("pc-alice", sec, "GET", path))
        check("普通用户可读取云端存档列表", r.status_code == 200, f"HTTP {r.status_code}")

        # 带 body 的签名请求：body 必须与签名时完全一致
        payload = {"base_version": 0, "files": [], "delete": [], "instance": "TestPack"}
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        path2 = "/api/save/PermWorld/commit"
        r = requests.post(f"{BASE}{path2}", timeout=10, data=body,
                          headers={"Content-Type": "application/json",
                                   **signed("pc-alice", sec, "POST", path2, body)})
        # 空清单会被服务端拒（400），但绝不能是 401/403 —— 那是鉴权没过
        check("普通用户上传请求通过鉴权（空清单只会 400）",
              r.status_code == 400, f"HTTP {r.status_code} {r.text[:80]}")

        # 普通用户不能删存档
        r = requests.delete(f"{BASE}/api/save/PermWorld", timeout=5,
                            headers=signed("pc-alice", sec, "DELETE", "/api/save/PermWorld"))
        check("普通用户删除存档被拒（403）", r.status_code == 403, f"HTTP {r.status_code}")

        # 普通用户不能访问账号管理
        r = requests.get(f"{BASE}/api/admin/accounts", timeout=5,
                         headers=signed("pc-alice", sec, "GET", "/api/admin/accounts"))
        check("普通用户访问账号管理被拒（403）", r.status_code == 403, f"HTTP {r.status_code}")

        # 管理员可以删
        r = s.delete(f"{BASE}/api/save/PermWorld", timeout=5)
        check("管理员删除成功或存档不存在", r.status_code in (200, 404),
              f"HTTP {r.status_code}")

        # ---------------------------------------------------- 6. 停用
        print("\n[6] 停用账号立即失效")
        r = s.post(f"{BASE}/api/admin/accounts/{USER[0]}/disabled", timeout=5,
                   json={"disabled": True})
        check("停用成功", r.status_code == 200, r.text[:120])
        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": USER[1],
            "client_id": "pc-alice", "secret": sec})
        check("停用后无法登录", r.status_code == 401, f"HTTP {r.status_code}")

        # 已登记的设备也立刻失效
        r = requests.get(f"{BASE}/api/saves", timeout=5,
                         headers=signed("pc-alice", sec, "GET", "/api/saves"))
        check("停用后旧设备凭证失效", r.status_code in (401, 403), f"HTTP {r.status_code}")

        r = s.post(f"{BASE}/api/admin/accounts/{USER[0]}/disabled", timeout=5,
                   json={"disabled": False})
        check("重新启用成功", r.status_code == 200, r.text[:120])
        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": USER[1],
            "client_id": "pc-alice", "secret": sec})
        check("启用后可再次登录", r.status_code == 200, f"HTTP {r.status_code}")

        # ---------------------------------------------------- 7. 最后一个管理员保护
        print("\n[7] 最后一个管理员受保护")
        r = s.post(f"{BASE}/api/admin/accounts/{ADMIN[0]}/role", timeout=5,
                   json={"role": "user"})
        check("唯一管理员不可被降级", r.status_code >= 400, f"HTTP {r.status_code}")
        r = s.delete(f"{BASE}/api/admin/accounts/{ADMIN[0]}", timeout=5)
        check("唯一管理员不可被删除", r.status_code >= 400, f"HTTP {r.status_code}")

        # 再建一个管理员：用新会话操作（原管理员删掉后其会话会失效，
        # 这是预期行为 —— 账号都不在了，会话当然要失效）
        s.post(f"{BASE}/api/admin/accounts", timeout=5, json={
            "username": "admin2", "password": "Admin2Pass", "role": "admin"})
        s2 = requests.Session()
        r = s2.post(f"{BASE}/api/web/login", timeout=5,
                    json={"username": "admin2", "password": "Admin2Pass"})
        check("第二个管理员可登录", r.status_code == 200, f"HTTP {r.status_code}")
        r = s.delete(f"{BASE}/api/admin/accounts/{ADMIN[0]}", timeout=5)
        check("存在第二个管理员时可删除", r.status_code == 200, f"HTTP {r.status_code}")
        r = s.get(f"{BASE}/api/me", timeout=5)
        check("被删管理员的会话立即失效", r.status_code in (401, 403),
              f"HTTP {r.status_code}")

        # ---------------------------------------------------- 8. 改口令
        print("\n[8] 改口令")
        r = s2.post(f"{BASE}/api/admin/accounts/{USER[0]}/password", timeout=5,
                    json={"password": "NewAlicePass"})
        check("改口令成功", r.status_code == 200, r.text[:120])
        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": "NewAlicePass",
            "client_id": "pc-alice", "secret": sec})
        check("新口令可用", r.status_code == 200, f"HTTP {r.status_code}")
        r = requests.post(f"{BASE}/api/login", timeout=5, json={
            "username": USER[0], "password": USER[1],
            "client_id": "pc-alice", "secret": sec})
        check("旧口令失效", r.status_code == 401, f"HTTP {r.status_code}")

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print("\n" + "=" * 58)
    bad = [n for n, ok_ in OK if not ok_]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 账号登录与两级权限正常")
    print("=" * 58)


if __name__ == "__main__":
    main()
