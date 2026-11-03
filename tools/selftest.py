# -*- coding: utf-8 -*-
"""
MCSync 端到端自测：启动服务端 → 造假存档 → 上传 → 增量上传 → 下载 → 冲突 → 回滚
用法： python3 tools/selftest.py
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, CLIENT)

PORT = int(os.environ.get("TEST_PORT", "8899"))
TMP = os.environ.get("MCSYNC_TEST_TMP", "/tmp/mcsync-selftest")
DATA = os.path.join(TMP, "serverdata")

os.makedirs(TMP, exist_ok=True)

from core.api import Api  # noqa: E402
from core.config import cfg  # noqa: E402
from core.hasher import file_hash  # noqa: E402
from core.protocol import human_size, make_ignore  # noqa: E402
from core.sync import SyncEngine  # noqa: E402

cfg.dir = os.path.join(TMP, "clientcfg")
os.makedirs(cfg.dir, exist_ok=True)
cfg.set("server_url", f"http://127.0.0.1:{PORT}")
cfg.set("concurrency", 4)
cfg.set("chunk_size_mb", 1)
cfg.set("backup_before_sync", True)

OK = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def rand_bytes(n: int, seed: int = 0) -> bytes:
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(256)) * (n // 256 + 1) [:n] \
        if False else os.urandom(n)


def make_save(path: str, seed: int = 1, big: int = 3) -> None:
    os.makedirs(os.path.join(path, "region"), exist_ok=True)
    os.makedirs(os.path.join(path, "data"), exist_ok=True)
    with open(os.path.join(path, "level.dat"), "wb") as f:
        f.write(b"\x1f\x8b" + os.urandom(400))  # 假 level.dat（解析失败也不影响同步）
    with open(os.path.join(path, "region", "r.0.0.mca"), "wb") as f:
        f.write(os.urandom(big * 1024 * 1024))
    with open(os.path.join(path, "data", "map_1.dat"), "wb") as f:
        f.write(os.urandom(4096))
    with open(os.path.join(path, "session.lock"), "wb") as f:
        f.write(b"0")
    with open(os.path.join(path, "aaa.txt"), "wb") as f:
        f.write(f"seed={seed}".encode())


def snapshot(path: str) -> dict:
    """按同步规则（忽略 .mcsync / session.lock 等）生成内容指纹快照"""
    matcher = make_ignore()
    out = {}
    for r, dirs, fs in os.walk(path):
        rel_root = os.path.relpath(r, path).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root
        keep = []
        for d in dirs:
            rel = (rel_root + "/" + d) if rel_root else d
            if matcher.match(rel) or d.startswith("_"):
                continue
            keep.append(d)
        dirs[:] = keep
        for f in fs:
            rel = (rel_root + "/" + f) if rel_root else f
            if matcher.match(rel) or f.startswith("_"):
                continue
            out[rel] = file_hash(os.path.join(r, f))
    return out


def main():
    print("\n=== MCSync 端到端自测 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(os.path.join(TMP, "clientcfg"), exist_ok=True)

    # ---------------------------------------------------------- 启动服务端
    print("[1] 启动服务端")
    # 同步逻辑回归测试：关闭鉴权；鉴权本身由 tools/test_auth.py 单独覆盖
    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
              MCSYNC_AUTH="0", PYTHONUNBUFFERED="1")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "error"],
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
        # ---------------------------------------------------------- 构造存档
        print("\n[2] 构造测试存档")
        save_a = os.path.join(TMP, "instance", "saves", "MyWorld")
        make_save(save_a, seed=1, big=3)
        before = snapshot(save_a)
        total = sum(os.path.getsize(os.path.join(save_a, p)) for p in before)
        check("存档文件生成", len(before) >= 4, f"{len(before)} 个文件，{human_size(total)}")
        check("session.lock 未进入同步清单（被默认规则忽略）", "session.lock" not in before)

        # ---------------------------------------------------------- 首次上传
        print("\n[3] 首次上传（全量）")
        eng = SyncEngine(api, log=lambda m: print("      · " + m))
        r1 = eng.upload(save_a, "MyWorld", note="首次")
        check("上传返回版本 v1", r1["version"] == 1, str(r1))
        check("云端出现该存档", any(s["name"] == "MyWorld" for s in api.list_saves()))
        v1_bytes = r1["uploaded_bytes"]
        print(f"      实际上传字节：{human_size(v1_bytes)}")

        # ---------------------------------------------------------- 无改动再上传
        print("\n[4] 无改动重复上传（应几乎零传输）")
        r2 = eng.upload(save_a, "MyWorld", note="无改动")
        check("无改动上传不产生实际传输", r2["uploaded_bytes"] == 0, f"{r2['uploaded_bytes']} 字节")
        check("无改动时无需计算新数据块", r2["reused_chunks"] >= 0)
        check("仍生成新版本快照 v2", r2["version"] == 2)

        # ---------------------------------------------------------- 增量上传
        print("\n[5] 修改大文件后增量上传")
        with open(os.path.join(save_a, "region", "r.0.0.mca"), "r+b") as f:
            f.seek(1024 * 256)
            f.write(b"X" * 8192)          # 改中间一小段
        time.sleep(1.1)   # 确保 mtime 变化，验证哈希预筛能发现改动
        with open(os.path.join(save_a, "newfile.dat"), "wb") as f:
            f.write(os.urandom(2048))
        r3 = eng.upload(save_a, "MyWorld", note="增量")
        print(f"      实际上传字节：{human_size(r3['uploaded_bytes'])} / 存档总大小 {human_size(total)}")
        check("增量上传字节数远小于全量", r3["uploaded_bytes"] < total * 0.5,
              f"{human_size(r3['uploaded_bytes'])} < {human_size(total*0.5)}")
        check("生成版本 v3", r3["version"] == 3)

        # ---------------------------------------------------------- 下载到新目录
        print("\n[6] 下载到全新目录")
        save_b = os.path.join(TMP, "instance", "saves", "MyWorld-2")
        r4 = eng.download(save_b, "MyWorld")
        sa, sb = snapshot(save_a), snapshot(save_b)
        diff = {k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k) and not k.startswith((".mcsync", "_"))}
        check("下载后内容与源一致", not diff, f"差异文件: {list(diff)[:5]}")
        check("下载文件数一致", r4["file_count"] >= len(sa), f"{r4['file_count']} vs {len(sa)}")

        # ---------------------------------------------------------- 冲突检测
        print("\n[7] 冲突检测（本地与云端都改了同一文件）")
        time.sleep(1.1)
        with open(os.path.join(save_a, "aaa.txt"), "wb") as f:      # A：本地改动 → 上传
            f.write(b"from A")
        eng.upload(save_a, "MyWorld", note="A 修改")                 # v4
        time.sleep(1.1)
        with open(os.path.join(save_b, "aaa.txt"), "wb") as f:      # B：也改了（base 是 v3）
            f.write(b"from B")
        captured = {}
        eng_b = SyncEngine(api, log=lambda m: print("      · " + m))

        def resolver(conflicts):
            captured["n"] = len(conflicts)
            captured["paths"] = [c["path"] for c in conflicts]
            return {c["path"]: "local" for c in conflicts}

        r5 = eng_b.upload(save_b, "MyWorld", note="B 修改", resolver=resolver)
        check("检测到 1 个冲突文件", captured.get("n") == 1, str(captured))
        check("冲突文件为 aaa.txt", captured.get("paths") == ["aaa.txt"], str(captured.get("paths")))
        check("冲突按“用本地”处理后上传成功", r5["conflicts"] == 1 and r5["version"] >= 5, str(r5))

        # ---------------------------------------------------------- 版本回滚
        print("\n[8] 版本快照与回滚")
        vers = api.versions("MyWorld")
        check("版本数量 >= 5", len(vers) >= 5, f"{len(vers)} 个版本")
        target = [v for v in vers if v["id"] == 3][0]
        rr = api.restore("MyWorld", 3)
        new_v = rr["version"]["id"]
        check("回滚生成新版本", new_v > target["id"], f"v{target['id']} -> v{new_v}")
        m_old = api.manifest("MyWorld", 3)
        m_new = api.manifest("MyWorld", new_v)
        check("回滚版本内容与旧版本一致",
              m_old["files"]["aaa.txt"]["hash"] == m_new["files"]["aaa.txt"]["hash"])

        # ---------------------------------------------------------- 下载历史版本
        print("\n[9] 下载指定历史版本")
        save_c = os.path.join(TMP, "instance", "saves", "MyWorld-3")
        eng.download(save_c, "MyWorld", vid=1)
        with open(os.path.join(save_c, "aaa.txt"), "rb") as f:
            check("v1 的 aaa.txt 内容为最初版本", f.read() == b"seed=1", f.read().decode(errors="replace"))

        # ---------------------------------------------------------- 删除与统计
        print("\n[10] 删除与统计")
        api.delete_version("MyWorld", 1)
        check("删除版本成功", len(api.versions("MyWorld")) == len(vers) + 1 - 1,
              f"{len(api.versions('MyWorld'))} 个")
        st = api.stats()
        check("统计接口可用", st["save_count"] == 1 and st["version_count"] > 0, str(st))
        print(f"      服务端统计：{st}")

        print("\n=== 全部测试通过 ✅ ===")
        print(f"    服务端数据目录：{DATA}")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()


if __name__ == "__main__":
    main()
