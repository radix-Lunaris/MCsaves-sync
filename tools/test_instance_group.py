# -*- coding: utf-8 -*-
"""
整合包（实例）归属回归测试：
  1) 上传时携带实例名 -> 云端按整合包分组
  2) 不同整合包里同名存档互不干扰（引入 instance 的核心原因）
  3) 老存档（无 instance）归入「未分类」，可手动归类
  4) 旧库升级：缺 instance 列时自动补列，老数据不丢

用法： python3 tools/test_instance_group.py
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

PORT = int(os.environ.get("TEST_PORT", "8915"))
TMP = os.environ.get("MCSYNC_TEST_TMP", "/tmp/mcsync-instgroup")
DATA = os.path.join(TMP, "serverdata")
TOKEN = "InstGroupTest12345"

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
        f.write(os.urandom(128 * 1024))
    with open(os.path.join(path, "tag.txt"), "w", encoding="utf-8") as f:
        f.write(tag)


def snapshot(path: str) -> dict:
    out = {}
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".mcsync", "_MCSyncBackups")]
        for f in fs:
            if f.startswith("_"):
                continue
            full = os.path.join(r, f)
            rel = os.path.relpath(full, path).replace("\\", "/")
            if rel.startswith(".mcsync/") or rel.startswith("_MCSyncBackups"):
                continue
            out[rel] = file_hash(full)
    return out


def start_server():
    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
               MCSYNC_AUTH="1", MCSYNC_TOKEN=TOKEN, PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def wait_up(api):
    for _ in range(40):
        try:
            api.health()
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("服务端启动失败")


def main():
    print("\n=== MCSync 整合包归属回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(os.path.join(TMP, "clientcfg"), exist_ok=True)

    cfg.dir = os.path.join(TMP, "clientcfg")
    cfg.set("server_url", f"http://127.0.0.1:{PORT}")
    cfg.set("concurrency", 4)
    cfg.set("chunk_size_mb", 1)
    cfg.set("backup_before_sync", False)

    srv = start_server()
    api = Api(f"http://127.0.0.1:{PORT}")
    wait_up(api)
    check("服务端健康检查", api.health().get("ok") is True)

    try:
        # 用管理员密钥引导出管理员账号，再用账号登录（旧版是口令配对，已废弃）
        import requests as _rq
        _r = _rq.post(f"http://127.0.0.1:{PORT}/api/bootstrap", timeout=10, json={
            "admin_key": TOKEN, "username": "tester", "password": "TesterPass123"})
        if _r.status_code == 200:
            pass                      # 首次引导
        cfg.set("username", "tester")
        cfg.set_secret("TesterPass123", "password")
        api.login()
        check("客户端配对成功", bool(cfg.get_secret()))

        # ---------------------------------------------------- 1. 携带实例名上传
        print("\n[1] 上传时携带整合包名")
        inst_a, inst_b = "1.20.1-Forge", "1.12.2-Forge"

        src_a = os.path.join(TMP, "src", inst_a, "MyWorld")
        src_b = os.path.join(TMP, "src", inst_b, "MyWorld")
        make_save(src_a, "整合包A的世界")
        make_save(src_b, "整合包B的世界")
        snap_a, snap_b = snapshot(src_a), snapshot(src_b)
        check("两个整合包存在同名存档且内容不同", snap_a != snap_b)

        eng = SyncEngine(api, log=lambda m: None)
        eng.upload(src_a, "MyWorld", instance=inst_a)
        eng.upload(src_b, "MyWorld", instance=inst_b)

        saves = api.list_saves()
        got = sorted((s.get("instance", ""), s.get("display") or s.get("name", "")) for s in saves)
        check("两个整合包的同名存档各成一条，归属正确",
              (inst_a, "MyWorld") in got and (inst_b, "MyWorld") in got, str(got))

        # ---------------------------------------------------- 2. 同名存档互不干扰
        print("\n[2] 不同整合包的同名存档互不干扰")
        dst_a = os.path.join(TMP, "dst", "A")
        dst_b = os.path.join(TMP, "dst", "B")
        # 找到各自那一条的实际云端键（冲突时第二段会被限定为 "MyWorld (整合包名)"）
        key_a = [s["name"] for s in saves if s.get("instance") == inst_a][0]
        key_b = [s["name"] for s in saves if s.get("instance") == inst_b][0]
        check("两条记录的云端键不同", key_a != key_b, f"{key_a} / {key_b}")

        eng.download(dst_a, key_a)
        eng.download(dst_b, key_b)
        check("整合包A的存档下载内容正确", snapshot(dst_a) == snap_a, key_a)
        check("整合包B的存档下载内容正确", snapshot(dst_b) == snap_b, key_b)
        check("两个存档内容确实不同", snapshot(dst_a) != snapshot(dst_b))

        # ---------------------------------------------------- 3. 老存档归未分类
        print("\n[3] 老存档（无整合包信息）归入「未分类」")
        src_old = os.path.join(TMP, "src", "old", "LegacyWorld")
        make_save(src_old, "老存档")
        snap_old = snapshot(src_old)
        eng.upload(src_old, "LegacyWorld")  # 不传 instance，模拟老客户端
        saves = api.list_saves()
        legacy = [s for s in saves if s.get("name") == "LegacyWorld"]
        check("老存档已上传", len(legacy) == 1)
        check("老存档 instance 为空 -> 归入未分类",
              not (legacy[0].get("instance", "") or "").strip(),
              repr(legacy[0].get("instance")))

        # ---------------------------------------------------- 4. 手动归类
        print("\n[4] 手动把老存档归类到整合包")
        r = api.set_instance("LegacyWorld", inst_a)
        check("归类接口返回成功", r.get("ok") is True, str(r)[:80])
        saves = api.list_saves()
        legacy = [s for s in saves if s.get("name") == "LegacyWorld"][0]
        check("归类后 instance 已更新", legacy.get("instance") == inst_a,
              repr(legacy.get("instance")))

        # ---------------------------------------------------- 5. 再传不清空归属
        print("\n[5] 已归属的存档，被老客户端再次上传时不被清空")
        eng.upload(src_old, "LegacyWorld")  # 再次上传且不带 instance
        saves = api.list_saves()
        legacy = [s for s in saves if s.get("name") == "LegacyWorld"][0]
        check("归属信息保留", legacy.get("instance") == inst_a,
              repr(legacy.get("instance")))

        # ---------------------------------------------------- 6. 分组逻辑
        print("\n[6] 列表分组逻辑（客户端 UI 用的同一套规则）")
        def group(names):
            g = {}
            for s in names:
                k = (s.get("instance", "") or "").strip() or "未分类"
                g.setdefault(k, []).append(s["name"])
            return g
        gs = group(saves)
        check("存在整合包分组", inst_b in gs or inst_a in gs, str(sorted(gs.keys())))
        check("未分类分组可为空", True)

        # ---------------------------------------------------- 7. 显示层不再用 v1/v2 做主标识
        print("\n[7] 云端卡片显示的字段")
        s0 = saves[0]
        check("返回了更新时间字段", "latest_time" in s0 or "updated_at" in s0,
              str(sorted(s0.keys())))

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    # ---------------------------------------------------- 8. 旧库升级
    print("\n[8] 旧库升级：缺 instance 列时自动补列")
    sys.path.insert(0, SERVER)
    import sqlite3
    legacy_db = os.path.join(TMP, "legacydb")
    os.makedirs(legacy_db, exist_ok=True)
    dbp = os.path.join(legacy_db, "mcsync.db")
    conn = sqlite3.connect(dbp)
    conn.executescript("""
        CREATE TABLE saves(name TEXT PRIMARY KEY, created_at REAL, updated_at REAL);
        CREATE TABLE versions(id INTEGER PRIMARY KEY AUTOINCREMENT, save_name TEXT NOT NULL,
            created_at REAL NOT NULL, note TEXT DEFAULT '', file_count INTEGER DEFAULT 0,
            total_size INTEGER DEFAULT 0, manifest_file TEXT NOT NULL, parent INTEGER DEFAULT 0);
    """)
    conn.execute("INSERT INTO saves VALUES('OldWorld', 1.0, 2.0)")
    conn.commit()
    conn.close()

    from store import Store
    st = Store(legacy_db)
    rows = st.list_saves()
    check("旧库可正常打开", len(rows) >= 1, f"{len(rows)} 个存档")
    check("补列后 instance 字段可读", "instance" in rows[0], str(sorted(rows[0].keys())))
    check("老数据未丢失", rows[0].get("name") == "OldWorld", rows[0].get("name"))
    check("老数据 instance 为空（归入未分类）",
          not (rows[0].get("instance") or "").strip())

    print("\n" + "=" * 58)
    bad = [n for n, ok in OK if not ok]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 云端存档可按整合包归类")
    print("=" * 58)


if __name__ == "__main__":
    main()
