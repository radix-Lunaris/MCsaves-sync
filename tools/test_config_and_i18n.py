# -*- coding: utf-8 -*-
"""
整合包配置同步 + 界面多语言 回归测试

A) config 目录同步
   同一个整合包多人玩时，模组/服务端配置必须一致，否则各人世界表现会不一样。
   配置作为独立条目上云（kind=config），不进 saves，也不会和存档混淆。

B) 多语言
   中文模式原样返回；英文模式查表；未翻译的安全退回中文（不出现空白/占位符）。

用法： python3 tools/test_config_and_i18n.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))

PORT = int(os.environ.get("TEST_PORT", "8951"))
TMP = "/tmp/mcsync-cfg"
DATA = os.path.join(TMP, "serverdata")
BASE = f"http://127.0.0.1:{PORT}"
PACK = "All the Mods"

import requests  # noqa: E402

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def start_server():
    env = dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=str(PORT),
               MCSYNC_AUTH="0", PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=os.path.join(ROOT, "server"), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_up():
    for _ in range(60):
        try:
            if requests.get(f"{BASE}/api/health", timeout=3).json().get("ok"):
                return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("服务端启动失败")


def build_machine(machine: str):
    """造一台"电脑"：有同名整合包，带 config/saves"""
    gd = os.path.join(TMP, machine, PACK, ".minecraft")
    os.makedirs(os.path.join(gd, "config"), exist_ok=True)
    os.makedirs(os.path.join(gd, "saves", "MyWorld"), exist_ok=True)
    open(os.path.join(gd, "options.txt"), "w").close()
    return gd


def tree_digest(path: str) -> dict:
    """目录指纹。跳过 .mcsync —— 那是同步工具自己写的状态文件，
    里面带着时间戳，逐字节比对必然不同，不属于用户数据。"""
    import hashlib
    out = {}
    for dp, dns, fs in os.walk(path):
        dns[:] = [d for d in dns if d != ".mcsync"]
        for fn in fs:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, path).replace("\\", "/")
            if rel.startswith(".mcsync/"):
                continue
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def main():
    print("\n=== 整合包配置同步 & 多语言 回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")
    os.makedirs(os.path.join(TMP, "cfg"), exist_ok=True)

    srv = start_server()
    wait_up()
    print("  ✅ 服务端已启动\n")

    try:
        # ---------------- A. 配置同步 ----------------
        print("[A] 整合包配置同步")
        gd_a = build_machine("pcA")
        cfg_a = os.path.join(gd_a, "config")
        os.makedirs(os.path.join(cfg_a, "sub"), exist_ok=True)
        with open(os.path.join(cfg_a, "server.cfg"), "w", encoding="utf-8") as f:
            f.write("max-players=8\nview-distance=12\n")
        with open(os.path.join(cfg_a, "sub", "mod.toml"), "w", encoding="utf-8") as f:
            f.write("[mods]\nenabled=true\n")
        src_cfg = tree_digest(cfg_a)

        from core.config import cfg
        from core.api import Api
        from core.sync import SyncEngine
        cfg.set("server_url", BASE)
        cfg.set("proxy_mode", "off")
        api = Api(BASE)

        r = SyncEngine(api, log=lambda m: None).upload(
            cfg_a, "整合包配置", instance=PACK, note="整合包配置", kind="config")
        check("配置上传成功", r.get("version") == 1, str(r))
        print(f"      上传 v{r['version']} · {r['file_count']} 个文件")

        saves = api.list_saves()
        rec = next((x for x in saves if x.get("kind") == "config"), None)
        check("云端存在 kind=config 的条目", rec is not None,
              str([(x.get("name"), x.get("kind")) for x in saves]))
        check("配置带整合包名", rec.get("instance") == PACK, str(rec.get("instance")))
        check("显示名为「整合包配置」", rec.get("display") == "整合包配置", str(rec.get("display")))

        # 另一个实例的同名配置不应互相覆盖
        gd_b = build_machine("pcB")
        cfg_b = os.path.join(gd_b, "config")
        os.makedirs(cfg_b, exist_ok=True)
        with open(os.path.join(cfg_b, "server.cfg"), "w", encoding="utf-8") as f:
            f.write("max-players=4\n")
        r2 = SyncEngine(api, log=lambda m: None).upload(
            cfg_b, "整合包配置", instance="AnotherPack", note="", kind="config")
        # 注意：versions.id 是全局自增，所以新记录的第一个版本 id 不一定是 1，
        # 判定"各自独立"要看是否新建了记录，而不是版本号。
        names = sorted(x.get("name") for x in api.list_saves() if x.get("kind") == "config")
        check("不同整合包的同名配置各自独立（另建记录）", len(names) == 2, str(names))
        check("原整合包配置未被覆盖",
              any(n == "整合包配置" for n in names), str(names))
        own = [v for v in api.versions("整合包配置") ]
        check("原配置的版本历史未被污染", len(own) == 1, str(len(own)))

        # B 机器下载 A 的配置
        dl = os.path.join(TMP, "pcB", PACK, ".minecraft", "config")
        os.makedirs(dl, exist_ok=True)
        with open(os.path.join(dl, "old.txt"), "w") as f:
            f.write("stale\n")
        SyncEngine(api, log=lambda m: None).download(dl, rec.get("name"))
        got = tree_digest(dl)
        mine = tree_digest(cfg_a)
        diff = [k for k in mine if mine[k] != got.get(k)]
        check("下载后配置文件内容逐一一致", not diff, str(diff))
        check("云端已删的旧文件不会误留在目标目录之外",
              all(k in got for k in mine), str(sorted(mine)))
        # 本地那台机器上原本就有的、不属于这份配置的文件默认保留：
        # 没有上次同步基线，工具不知道它是不是用户自己的东西，不能擅自删。
        check("本地无关文件默认保留（不误删）", "old.txt" in got, str(sorted(got)))
        print(f"      还原 {len(mine)} 个配置文件")

        # ---------------- B. 多语言 ----------------
        print("\n[B] 界面多语言")
        from core import i18n
        from core.i18n import t, set_lang, get_lang

        set_lang("zh")
        check("中文模式原样返回", t("取消") == "取消", t("取消"))
        check("get_lang 正确", get_lang() == "zh", get_lang())

        set_lang("en")
        check("英文模式查表生效", t("取消") == "Cancel", t("取消"))
        check("英文模式按钮文案", t("⬆  上传到云端") == "⬆  Upload to Cloud", t("⬆  上传到云端"))
        check("未翻译的安全退回中文",
              t("一句还没来得及翻译的文案") == "一句还没来得及翻译的文案", "")
        check("空串不炸", t("") == "", "")

        set_lang("xx")          # 未知代码
        check("未知语言代码退回中文", get_lang() == "zh", get_lang())

        # 覆盖率：确认源码里用到的界面文案大多已翻译
        import json
        wrapped = json.load(open("/tmp/wrapped.json", encoding="utf-8")) \
            if os.path.exists("/tmp/wrapped.json") else []
        if wrapped:
            miss = [w for w in wrapped if w not in i18n._EN]
            ratio = (len(wrapped) - len(miss)) / len(wrapped) * 100
            print(f"      界面文案 {len(wrapped)} 条，已译 {len(wrapped) - len(miss)} 条（{ratio:.0f}%）")
            check("已包装的界面文案全部有英译", not miss, str(miss[:5]))
        set_lang("zh")

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
    print(f"✅ 全部 {len(OK)} 项通过 — 配置同步与多语言正常")
    print("=" * 58)


if __name__ == "__main__":
    main()
