# -*- coding: utf-8 -*-
"""
设置持久化 & 启动性能 回归测试

A) 设置持久化
   用户在设置里改的每一项，关掉软件再打开都必须还在。
   曾经出现过"改了没保存 / 重启回默认"的问题，这里把关键项逐个往返验证。

B) 启动性能
   启动时的实例重扫曾慢到"一直转圈"。根因有两个：
     1. 存档列表用 quick=False，会遍历存档下每个文件统计大小
     2. 重扫结果即使没变也重建界面，进而重新选中实例、重新加载存档
   这里验证 quick 与全量的耗时差距、以及"结果未变则不重建"的判断。

用法： python3 tools/test_settings_and_perf.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
TMP = "/tmp/mcsync-settings"
os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def main():
    print("\n=== 设置持久化 & 启动性能 回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "cfg"), exist_ok=True)

    from core.config import cfg

    # ---------------- A. 设置往返持久化 ----------------
    print("[A] 设置项往返持久化（写入 → 重载 → 校验）")
    cases = {
        "server_url": "http://10.0.0.7:28650",
        "theme_mode": "light",
        "ui_scale": 1.25,
        "auto_scale": False,
        "scan_depth": 5,
        "chunk_size_mb": 8,
        "concurrency": 6,
        "ignore_rules": ["*.log", "backups/"],
        "custom_roots": ["/tmp/mcsync-settings/inst"],
        "keep_backups": 3,
        "auto_sync_enabled": True,
        "auto_sync_minutes": 45,
        "allow_sync_while_running": True,
        "backup_before_sync": False,
        "delete_remote_on_upload": False,
        "delete_local_on_download": False,
        "core_only": True,
        "force_full_hash": True,
        "language": "en",
        "remember_password": True,
        "auto_login": True,
    }
    for k, v in cases.items():
        cfg.set(k, v)
    cfg.save()

    # 模拟"关掉软件再打开"：重新加载
    cfg.data.clear()
    cfg.load()
    bad = []
    for k, v in cases.items():
        got = cfg.get(k)
        if isinstance(v, list):
            if list(got or []) != v:
                bad.append((k, v, got))
        elif got != v:
            bad.append((k, v, got))
    check(f"{len(cases)} 项设置全部持久化", not bad, str(bad[:3]))

    # ---------------- B. 口令加密存取 ----------------
    print("\n[B] 登录口令的安全存放")
    cfg.set_secret("MySecretPw123", "password")
    check("口令可存可取", cfg.get_secret("password") == "MySecretPw123", "")
    # 明文不能出现在配置文件里
    cfg.save()
    raw = open(cfg.path, encoding="utf-8").read() if os.path.exists(cfg.path) else ""
    check("口令未以明文写入配置文件", "MySecretPw123" not in raw, "")
    cfg.set_secret("", "password")
    check("可清除口令（退出登录）", cfg.get_secret("password") == "", "")

    # ---------------- C. 启动性能 ----------------
    print("\n[C] 启动性能")
    from core.scanner import InstanceScanner
    from core.models import Instance

    # 造一个规模接近真实存档的目录（3000 个文件）
    inst_root = os.path.join(TMP, "inst")
    gd = os.path.join(inst_root, ".minecraft")
    world = os.path.join(gd, "saves", "BigWorld")
    os.makedirs(os.path.join(world, "region"), exist_ok=True)
    with open(os.path.join(world, "level.dat"), "wb") as f:
        f.write(b"\x1f\x8b" + os.urandom(2048))
    for i in range(3000):
        with open(os.path.join(world, "region", f"r.{i % 32}.{i // 32}.mca"), "wb") as f:
            f.write(os.urandom(4096))
    inst = Instance(name="Big", root=inst_root, game_dir=gd, source="测试")

    t = time.time()
    s_quick = InstanceScanner.list_saves(inst, quick=True)
    t_quick = time.time() - t
    t = time.time()
    s_full = InstanceScanner.list_saves(inst, quick=False)
    t_full = time.time() - t
    print(f"      quick(True) {t_quick*1000:.0f}ms · quick(False) {t_full*1000:.0f}ms")
    check("两种模式都能列出存档", len(s_quick) == 1 and len(s_full) == 1,
          f"{len(s_quick)}/{len(s_full)}")
    check("quick 模式明显更快（启动走这条路）", t_quick < t_full,
          f"{t_quick*1000:.0f}ms vs {t_full*1000:.0f}ms")
    check("quick 模式不做体积统计（留待后台补）",
          all(getattr(x, "size", -1) < 0 for x in s_quick),
          str([getattr(x, "size", None) for x in s_quick]))
    check("全量模式确实统计出体积",
          all(getattr(x, "size", 0) > 0 for x in s_full),
          str([getattr(x, "size", None) for x in s_full]))

    # ---------------- D. 重扫"结果未变则不重建" ----------------
    print("\n[D] 重扫无变化时跳过重建")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import ui_smoke as smoke  # 安装 tkinter 桩件
    smoke.install_stubs()
    import ui.main_window as mw

    class Holder:
        def __init__(self):
            self.instances = [
                Instance(name="A", root="/tmp/a", game_dir="/tmp/a/.minecraft", source="s"),
                Instance(name="B", root="/tmp/b", game_dir="/tmp/b/.minecraft", source="s"),
            ]
    H = Holder()
    H._same_instances = mw.App._same_instances.__get__(H)
    same = [Instance(name="B", root="/tmp/b", game_dir="/tmp/b/.minecraft", source="s"),
            Instance(name="A", root="/tmp/a", game_dir="/tmp/a/.minecraft", source="s")]
    check("顺序不同但集合相同 → 判定无变化",
          H._same_instances(same) is True, "")
    diff = [Instance(name="A", root="/tmp/a", game_dir="/tmp/a/.minecraft", source="s"),
            Instance(name="C", root="/tmp/c", game_dir="/tmp/c/.minecraft", source="s")]
    check("新增实例 → 判定有变化", H._same_instances(diff) is False, "")
    check("空结果 → 判定有变化（不该静默清空）",
          H._same_instances([]) is False, "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 设置可持久化，启动不再卡顿")
    print("=" * 58)


if __name__ == "__main__":
    main()
