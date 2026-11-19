# -*- coding: utf-8 -*-
"""
共享玩家数据 · 端到端测试（真实服务端 + 真实同步引擎）

主路径已改为「上传时自动摘除」：
  A 上传 → 本地与云端都变共享友好 → B 下载无需任何修正 → 各按 UUID 读自己的角色。

验证：
  1. 上传前自动摘除内嵌 Player 并归档，其余世界数据完好
  2. 云端版本确实干净（下载到独立目录复核）
  3. B 下载时已是 already_clean，无需再修
  4. 关掉开关后上传/下载都原样保留（可回退）
  5. 历史带房主数据的云端版本，开启开关下载时会兜底修正
  6. 填 my_uuid 且属于自己时，上传原样保留

用法： python3 tools/test_playerdata_sync.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, CLIENT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

PORT = int(os.environ.get("TEST_PORT", "8942"))
TMP = "/tmp/mcsync-pd-sync"
DATA = os.path.join(TMP, "serverdata")
BASE = f"http://127.0.0.1:{PORT}"
OWNER = uuid.UUID("c06f8906-4c8a-4911-9c71-e3b1f2a0d5e7")

from core import nbt_typed as N          # noqa: E402
from core import playerdata as pd        # noqa: E402
from core.api import Api                 # noqa: E402
from core.config import cfg              # noqa: E402
from core.sync import SyncEngine         # noqa: E402
import test_playerdata as helper         # noqa: E402

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def level_data(save_dir):
    _n, root, _k = N.read_file(os.path.join(save_dir, "level.dat"))
    return N.compound(N.child(root, "Data"))


def has_player(save_dir):
    try:
        return "Player" in level_data(save_dir)
    except Exception:
        return False


def make_save(save_dir: str, tag: str, legacy: bool = True):
    os.makedirs(os.path.join(save_dir, "region"), exist_ok=True)
    helper.make_level(os.path.join(save_dir, "level.dat"),
                      "legacy" if legacy else "clean", OWNER)
    with open(os.path.join(save_dir, "region", "r.0.0.mca"), "wb") as f:
        f.write((tag * 100).encode() + bytes(range(256)) * 8)
    with open(os.path.join(save_dir, "icon.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + tag.encode() * 64)
    os.makedirs(os.path.join(save_dir, "data"), exist_ok=True)
    with open(os.path.join(save_dir, "data", "scoreboard.dat"), "wb") as f:
        f.write(b"score-" + tag.encode())
    return save_dir


def main():
    print("\n=== 上传时自动摘除共享玩家数据 · 端到端 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)

    old_dir, old_data = cfg.dir, dict(cfg.data)
    cfg.dir = os.path.join(TMP, "cfg")
    os.makedirs(cfg.dir, exist_ok=True)
    cfg.set("server_url", BASE)
    cfg.set("auto_fix_playerdata", True)
    cfg.set("my_uuid", "")
    cfg.set("delete_local_on_download", True)
    cfg.set("ignore_rules", [])

    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
               MCSYNC_AUTH="0", PYTHONUNBUFFERED="1")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    api = Api(BASE)
    for _ in range(50):
        try:
            if api.health().get("ok"):
                break
        except Exception:
            time.sleep(0.3)
    else:
        # 必须先终止子进程再读 stdout，否则 read() 会一直等到进程结束，
        # 把「启动失败」变成「测试静默卡死」。
        srv.terminate()
        try:
            srv.wait(timeout=3)
        except Exception:
            srv.kill()
        try:
            out = (srv.stdout.read() or b"").decode(errors="replace")[-2000:]
        except Exception:
            out = ""
        print(out)
        raise SystemExit("服务端启动失败")

    try:
        # ---------------- 1. 上传时自动摘除 ----------------
        print("[1] A 上传 → 自动摘除房主内嵌数据")
        a = make_save(os.path.join(TMP, "A", "saves", "MyWorld"), "A")
        check("上传前含 Player", has_player(a), "")
        eng = SyncEngine(api, log=lambda m: print("      · " + m))
        r_up = eng.upload(a, "MyWorld", instance="1.20.1-Forge")
        check("上传成功", r_up.get("ok") is True, str(r_up.get("version")))
        check("A 本地 level.dat 已自动摘除 Player", not has_player(a), "")
        check("世界名等字段仍在",
              N.as_str(level_data(a).get("LevelName")) == "共享世界", "")
        arch = os.path.join(a, "playerdata", str(OWNER) + ".dat")
        check("房主数据已归档成独立 .dat", os.path.isfile(arch), arch)
        _n, aroot, _k = N.read_file(arch)
        check("归档含背包与经验",
              {"Inventory", "XpLevel", "Pos"} <= set(N.compound(aroot)), "")
        check("原始 level.dat 有备份",
              os.path.isdir(os.path.join(a, ".mcsync", "playerdata_backup")), "")

        # ---------------- 2. 云端版本确实干净 ----------------
        print("\n[2] 云端版本复核（独立目录下载）")
        verify = os.path.join(TMP, "verify", "saves", "MyWorld")
        eng.download(verify, "MyWorld")
        check("云端 level.dat 不含 Player", not has_player(verify), "")
        check("云端也带上了房主归档数据",
              os.path.isfile(os.path.join(verify, "playerdata", str(OWNER) + ".dat")), "")

        # ---------------- 3. B 下载无需修正 ----------------
        print("\n[3] B 下载 → 打开即自己的角色")
        b = os.path.join(TMP, "B", "saves", "MyWorld")
        r_dl = eng.download(b, "MyWorld")
        info = r_dl.get("playerdata") or {}
        check("下载结果为 already_clean",
              info.get("status") == "already_clean", str(info))
        check("B 的 level.dat 无 Player", not has_player(b), "")
        check("region 与 A 一致",
              open(os.path.join(a, "region", "r.0.0.mca"), "rb").read() ==
              open(os.path.join(b, "region", "r.0.0.mca"), "rb").read(), "")
        check("scoreboard.dat 完好",
              open(os.path.join(a, "data", "scoreboard.dat"), "rb").read() ==
              open(os.path.join(b, "data", "scoreboard.dat"), "rb").read(), "")
        check("二次下载幂等，归档不丢",
              eng.download(b, "MyWorld").get("ok") is True and
              os.path.isfile(os.path.join(b, "playerdata", str(OWNER) + ".dat")), "")

        # ---------------- 4. 关闭开关后完全不动 ----------------
        print("\n[4] 关闭自动处理后上传/下载都原样")
        cfg.set("auto_fix_playerdata", False)
        a2 = make_save(os.path.join(TMP, "A2", "saves", "LegacyWorld"), "A2")
        eng.upload(a2, "LegacyWorld", instance="1.12.2-Forge")
        check("A2 本地仍含 Player", has_player(a2), "")
        d = os.path.join(TMP, "D", "saves", "LegacyWorld")
        r_dl4 = eng.download(d, "LegacyWorld")
        check("D 下载回来仍含 Player", has_player(d), "")
        check("没有生成修正结果", r_dl4.get("playerdata") is None,
              str(r_dl4.get("playerdata")))

        # ---------------- 5. 下载兜底修历史版本 ----------------
        print("\n[5] 开启开关后，下载历史带房主数据的版本会兜底修正")
        cfg.set("auto_fix_playerdata", True)
        e = os.path.join(TMP, "E", "saves", "LegacyWorld")
        r_dl5 = eng.download(e, "LegacyWorld")
        check("兜底修正生效",
              (r_dl5.get("playerdata") or {}).get("status") == "fixed",
              str(r_dl5.get("playerdata")))
        check("E 的 level.dat 已无 Player", not has_player(e), "")
        check("E 也保留了房主归档",
              os.path.isfile(os.path.join(e, "playerdata", str(OWNER) + ".dat")), "")

        # ---------------- 6. 属于自己时上传保留 ----------------
        print("\n[6] 填 my_uuid 且内嵌数据属于自己时原样保留")
        cfg.set("my_uuid", str(OWNER))
        a3 = make_save(os.path.join(TMP, "A3", "saves", "MyOwnWorld"), "A3")
        eng.upload(a3, "MyOwnWorld", instance="1.20.1-Forge")
        check("A3 本地仍含 Player", has_player(a3), "")
        check("没有生成归档目录",
              not os.path.isdir(os.path.join(a3, "playerdata")), "")
        cfg.set("my_uuid", "")

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        cfg.dir = old_dir
        cfg.data = old_data

    print("\n" + "=" * 60)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 上传即让云端存档共享友好")
    print("=" * 60)


if __name__ == "__main__":
    main()
