# -*- coding: utf-8 -*-
"""
多人协作端到端测试：起真实服务端 + 真实客户端，模拟两台电脑。

场景：
  机器 A ── 整合包「All the Mods」，存档 MyWorld ── 上传
  机器 B ── 也有整合包「All the Mods」，但没有这个存档 ── 下载
  期望：B 下载后装到 B 本机 All the Mods/saves/MyWorld，内容与 A 完全一致。

同时验证：
  - 上传时整合包名随存档一起上云
  - 普通用户能完成完整同步（上传 + 下载）
  - 普通用户删云端存档被拒，管理员可以删

用法： python3 tools/test_multiuser_e2e.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, CLIENT)

PORT = int(os.environ.get("TEST_PORT", "8931"))
TMP = "/tmp/mcsync-multiuser"
DATA = os.path.join(TMP, "serverdata")
CFG_A = os.path.join(TMP, "cfgA")
CFG_B = os.path.join(TMP, "cfgB")
ADMIN_KEY = "MultiKey123456789"
USER_A = ("alice", "AlicePass123")
USER_B = ("bob", "BobPass123")

PACK = "All the Mods"

import requests  # noqa: E402

OK: list = []
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
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_up():
    for _ in range(60):
        try:
            if requests.get(f"{BASE}/api/health", timeout=3).json().get("ok"):
                return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("服务端启动失败")


def make_machine(cfg_dir: str, machine: str, pack: str, save: str, seed: int) -> str:
    """造一台"电脑"：cfg/<machine>/<pack>/.minecraft/saves/<save>"""
    root = os.path.join(cfg_dir, machine, pack, ".minecraft")
    sd = os.path.join(root, "saves", save)
    os.makedirs(os.path.join(sd, "region"), exist_ok=True)
    open(os.path.join(sd, "level.dat"), "wb").write(os.urandom(4096))
    open(os.path.join(sd, "region", "r.0.0.mca"), "wb").write(os.urandom(3000))
    with open(os.path.join(sd, "icon.png"), "wb") as f:
        f.write(bytes([seed]) * 500)
    # 0 字节文件：曾经导致 409 missing_chunks，必须能正确往返
    # （不能用 session.lock —— 锁文件属于默认忽略规则，本就不参与同步）
    os.makedirs(os.path.join(sd, "data"), exist_ok=True)
    open(os.path.join(sd, "data", "empty.dat"), "wb").close()
    return sd


def digest_tree(path: str) -> dict:
    """目录内容指纹：相对路径 -> sha256。

    只统计"参与同步"的文件 —— 锁文件、日志等属于默认忽略规则，
    本来就不该在两台机器间传递，比对时要排除，否则会误报。
    """
    from core.protocol import make_ignore
    ig = make_ignore([], False)
    out = {}
    for dirpath, _dn, files in os.walk(path):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, path).replace("\\", "/")
            if ig.match(rel):
                continue
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def client(cfg_dir: str, username: str, password: str):
    """构造一个独立的客户端实例（各自的配置目录 = 各自的"电脑"）"""
    from core.api import Api
    from core.config import cfg
    from core.sync import SyncEngine
    cfg.dir = cfg_dir
    os.makedirs(cfg.dir, exist_ok=True)
    cfg.load()
    cfg.set("proxy_mode", "off")           # 测试环境无代理
    cfg.set("server_url", BASE)
    cfg.set("concurrency", 4)
    cfg.set("username", username)
    cfg.set_secret(password, "password")
    api = Api(BASE)
    api.login(username, password, name=username)
    return api, cfg, SyncEngine


def main():
    global BASE
    BASE = f"http://127.0.0.1:{PORT}"
    print("\n=== 多人协作端到端测试（两台电脑 + 真实服务端）===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)

    srv = start_server()
    wait_up()
    print("  ✅ 服务端已启动\n")

    try:
        # ---- 0. 建两个普通账号
        print("[0] 准备账号")
        r = requests.post(f"{BASE}/api/bootstrap", timeout=5, json={
            "admin_key": ADMIN_KEY, "username": "root", "password": "RootPass123"})
        check("引导管理员", r.status_code == 200, r.text[:80])
        s = requests.Session()
        s.post(f"{BASE}/api/web/login", timeout=5,
               json={"username": "root", "password": "RootPass123"})
        for u, p in (USER_A, USER_B):
            r = s.post(f"{BASE}/api/admin/accounts", timeout=5,
                       json={"username": u, "password": p, "role": "user"})
            check(f"创建账号 {u}", r.status_code == 200, r.text[:80])

        # ---- 1. 机器 A 上传
        print("\n[1] 机器 A 上传存档")
        a_save = make_machine(CFG_A, "pcA", PACK, "MyWorld", 1)
        src_fp = digest_tree(a_save)
        print(f"      源目录 {len(src_fp)} 个文件（含 1 个空文件）")

        api_a, cfg_a, SE = client(CFG_A, *USER_A)
        check("A 登录成功", bool(api_a.me), str(api_a.me))
        res = SE(api_a, log=lambda m: None).upload(a_save, "MyWorld", instance=PACK)
        check("A 上传成功", res.get("version") == 1, str(res))
        print(f"      上传 v{res['version']} · {res['file_count']} 文件")

        # ---- 2. 云端确实记录了整合包名
        print("\n[2] 云端记录整合包名")
        saves = api_a.list_saves()
        rec = next((x for x in saves if "MyWorld" in x.get("name", "")), None)
        check("云端能查到该存档", rec is not None, str([x.get("name") for x in saves]))
        check("云端记录了整合包名", rec.get("instance") == PACK, str(rec.get("instance")))
        cloud_name = rec.get("name")
        check("存档显示名为原始名", rec.get("display") == "MyWorld", str(rec.get("display")))
        print(f"      云端键 {cloud_name!r} · 整合包 {rec.get('instance')!r}")

        # ---- 3. 机器 B 下载并自动归位
        print("\n[3] 机器 B 下载（应自动装到同名整合包）")
        # B 本机也有 All the Mods，但没有 MyWorld
        b_root = os.path.join(CFG_B, "pcB", PACK, ".minecraft")
        os.makedirs(os.path.join(b_root, "saves"), exist_ok=True)
        open(os.path.join(b_root, "options.txt"), "w").close()

        api_b, cfg_b, _ = client(CFG_B, *USER_B)
        check("B 登录成功", bool(api_b.me), str(api_b.me))

        # 用与 App 一致的归位逻辑算出目标目录
        from core.models import Instance
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ui_smoke                       # 安装 tkinter 桩件
        ui_smoke.install_stubs()
        import ui.main_window as mw

        b_inst = Instance(name=PACK, root=os.path.join(CFG_B, "pcB", PACK),
                          game_dir=b_root, source="测试")

        class Place:
            instances = [b_inst]
            cur_instance = b_inst
            cloud = saves
            logs = []

            def log(self, m):
                self.logs.append(m)

            _cloud_record = mw.App._cloud_record
            _norm_inst = staticmethod(mw.App._norm_inst)
            _find_instance_by_name = mw.App._find_instance_by_name
            _resolve_download_target = mw.App._resolve_download_target

        target = Place()._resolve_download_target(cloud_name)
        expect = os.path.join(b_root, "saves", "MyWorld")
        check("归位到 B 本机同名整合包的 saves 目录", target == expect, f"{target}")
        check("自动归位，无需手动选择", target is not None, "")

        from core.sync import SyncEngine as SE2
        r = SE2(api_b, log=lambda m: None).download(target, cloud_name)
        check("B 下载成功", r.get("downloaded_files", 0) > 0, str(r))
        print(f"      下载 v{r['version']} · 写入 {r['downloaded_files']} 个文件")

        # ---- 4. 内容逐文件比对
        print("\n[4] 内容比对")
        dst_fp = digest_tree(target)
        check("文件数量一致", len(dst_fp) == len(src_fp),
              f"{len(dst_fp)} vs {len(src_fp)}")
        diff = [k for k in src_fp if src_fp.get(k) != dst_fp.get(k)]
        check("所有文件内容逐字节一致", not diff, str(diff[:5]))
        ep = os.path.join(target, "data", "empty.dat")
        check("0 字节文件被正确还原（存在且为 0 字节）",
              os.path.exists(ep) and os.path.getsize(ep) == 0,
              f"exists={os.path.exists(ep)}")

        # ---- 5. 权限：普通用户不能删，管理员可以
        print("\n[5] 权限分级")
        try:
            api_b.delete_save(cloud_name)
            deleted = True
        except Exception as e:
            deleted = False
            err = str(e)
        check("普通用户删除云端存档被拒", not deleted, "" if deleted else str(err)[:80])

        r = s.delete(f"{BASE}/api/save/{requests.utils.quote(cloud_name)}", timeout=5)
        check("管理员可删除云端存档", r.status_code == 200, f"HTTP {r.status_code}")

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
    print(f"✅ 全部 {len(OK)} 项通过 — 多人协作上传/下载/归位正常")
    print("=" * 58)


if __name__ == "__main__":
    main()
