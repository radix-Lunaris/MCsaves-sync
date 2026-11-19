# -*- coding: utf-8 -*-
"""
存档名编码回归测试：中文名 / 带空格 / 特殊字符的存档，在开启鉴权时必须能正常管理。

背景（真实 bug）：
  客户端按 HTTP 请求行签名，用的是百分号编码路径 /api/save/%E6%88%91.../versions；
  服务端 ASGI 拿到的是解码后的 /api/save/我的世界存档/versions。
  两者 HMAC 基串不同 -> bad_sign -> 客户端显示"私钥验证失败"。
  纯英文名的存档不受影响，所以现象是"新上传的能管理，老存档（中文名）不能"。

用法： python3 tools/test_name_encoding.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, CLIENT)

PORT = int(os.environ.get("TEST_PORT", "8912"))
TMP = os.environ.get("MCSYNC_TEST_TMP", "/tmp/mcsync-nameenc")
DATA = os.path.join(TMP, "serverdata")
TOKEN = "NameEncTest123456"

from core.api import Api  # noqa: E402
from core.config import cfg  # noqa: E402
from core.hasher import file_hash  # noqa: E402
from core.sync import SyncEngine  # noqa: E402

OK = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def make_save(path: str, tag: str) -> None:
    os.makedirs(os.path.join(path, "region"), exist_ok=True)
    with open(os.path.join(path, "level.dat"), "wb") as f:
        f.write(b"\x1f\x8b" + os.urandom(300))
    with open(os.path.join(path, "region", "r.0.0.mca"), "wb") as f:
        f.write(os.urandom(256 * 1024))
    with open(os.path.join(path, "tag.txt"), "w", encoding="utf-8") as f:
        f.write(tag)


def snapshot(path: str) -> dict:
    out = {}
    for r, dirs, fs in os.walk(path):
        # 排除同步状态目录与备份目录（这些是客户端自己生成的，不属于存档内容）
        dirs[:] = [d for d in dirs if d not in (".mcsync", "_backup", "_backups")]
        for f in fs:
            if f.startswith("_"):
                continue
            full = os.path.join(r, f)
            rel = os.path.relpath(full, path).replace("\\", "/")
            if rel.startswith(".mcsync/") or rel.startswith("_backup"):
                continue
            out[rel] = file_hash(full)
    return out


def raw_sign_request(base: str, cid: str, sec: str, method: str, path: str):
    """手工构造一次带签名的请求（模拟客户端行为：签名覆盖编码后的 path）"""
    import requests
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body = b""
    msg = "\n".join([ts, nonce, method, path, hashlib.sha256(body).hexdigest()])
    sig = hmac.new(sec.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return requests.get(base + path, headers={
        "X-MCSync-Key": cid, "X-MCSync-Ts": ts,
        "X-MCSync-Nonce": nonce, "X-MCSync-Sign": sig,
    }, timeout=15)


def main():
    print("\n=== MCSync 存档名编码回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(os.path.join(TMP, "clientcfg"), exist_ok=True)

    cfg.dir = os.path.join(TMP, "clientcfg")
    cfg.set("server_url", f"http://127.0.0.1:{PORT}")
    cfg.set("concurrency", 4)
    cfg.set("chunk_size_mb", 1)
    cfg.set("backup_before_sync", False)

    print("[1] 启动服务端（开启鉴权）")
    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
               MCSYNC_AUTH="1", MCSYNC_TOKEN=TOKEN, PYTHONUNBUFFERED="1")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    api = Api(f"http://127.0.0.1:{PORT}")
    for _ in range(40):
        try:
            api.health()
            break
        except Exception:
            time.sleep(0.3)
    else:
        print(srv.stdout.read().decode()[-2000:])
        raise SystemExit("服务端启动失败")
    check("服务端健康检查", api.health().get("ok") is True)

    try:
        print("\n[2] 配对客户端")
        # 用管理员密钥引导出管理员账号，再用账号登录（旧版是口令配对，已废弃）
        import requests as _rq
        _r = _rq.post(f"http://127.0.0.1:{PORT}/api/bootstrap", timeout=10, json={
            "admin_key": TOKEN, "username": "tester", "password": "TesterPass123"})
        if _r.status_code == 200:
            pass                      # 首次引导
        cfg.set("username", "tester")
        cfg.set_secret("TesterPass123", "password")
        api.login()
        cid = cfg.get("client_id", "")
        sec = cfg.get_secret()
        check("配对成功并拿到私钥", bool(cid) and bool(sec))

        names = [
            ("中文存档名", "我的世界存档"),
            ("带空格", "New World"),
            ("含括号", "存档(测试)"),
            ("纯英文基线", "NormalWorld"),
        ]

        for label, nm in names:
            print(f"\n[3] 存档名：{label} — {nm!r}")

            # --- 手工签名请求（签名覆盖编码后的路径，等价于真实客户端行为）
            r = raw_sign_request(f"http://127.0.0.1:{PORT}", cid, sec,
                                 "GET", f"/api/save/{quote(nm)}/versions")
            check(f"  版本管理接口不再报 bad_sign",
                  r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
            if r.status_code != 200:
                print(f"     响应: {r.text[:120]}")
                continue

            # --- 完整上传 -> 管理 -> 下载
            src = os.path.join(TMP, "src", nm)
            make_save(src, nm)
            before = snapshot(src)

            eng = SyncEngine(api, log=lambda m: None)
            res = eng.upload(src, nm)
            check("  上传成功", bool(res), str(res)[:80])

            vers = api.versions(nm)
            check("  能读取版本列表", len(vers) >= 1, f"{len(vers)} 个版本")

            dst = os.path.join(TMP, "dst", nm)
            eng.download(dst, nm)
            after = snapshot(dst)
            check("  下载后内容一致", before == after,
                  f"{len(before)} vs {len(after)} 个文件")

        # --- 安全性：篡改签名仍必须被拒绝
        print("\n[4] 安全校验：篡改签名必须仍被拒绝")
        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        p = "/api/save/%E6%88%91/versions"
        msg = "\n".join([ts, nonce, "GET", p, hashlib.sha256(b"").hexdigest()])
        bad = hmac.new((sec + "x").encode(), msg.encode(), hashlib.sha256).hexdigest()
        import requests
        r = requests.get(f"http://127.0.0.1:{PORT}" + p, headers={
            "X-MCSync-Key": cid, "X-MCSync-Ts": ts,
            "X-MCSync-Nonce": nonce, "X-MCSync-Sign": bad}, timeout=15)
        check("  错误签名仍返回 401", r.status_code == 401, f"HTTP {r.status_code}")

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print("\n" + "=" * 56)
    bad = [n for n, ok in OK if not ok]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 中文名/空格名存档均可正常管理")
    print("=" * 56)


if __name__ == "__main__":
    main()
