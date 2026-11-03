# -*- coding: utf-8 -*-
"""
两个关键修复的回归测试：

  A) 实例重扫结果必须落盘并与缓存合并
     旧版本只有"全盘重扫"才写缓存，点「实例重扫」扫到的新实例只活在内存里，
     下次启动走 quick 又从旧缓存恢复 —— 表现就是"重扫完一刷新又变回原样"。

  B) 云端存档按所属整合包名自动归位
     多人使用时：A 在整合包 X 里上传存档，B 下载时应自动装到 B 本机
     同名整合包 X 的 saves 目录下，并还原成原始存档名。

用法： python3 tools/test_rescan_and_place.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
sys.path.insert(0, CLIENT)

# 复用 ui_smoke 的 tkinter 桩件，保证 ui.main_window 能在无 GUI 环境下导入
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ui_smoke  # noqa: E402,F401
ui_smoke.install_stubs()

from core.models import Instance  # noqa: E402

OK: list = []
TMP = "/tmp/mcsync-rescan"


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def mk_instance(base: str, name: str) -> Instance:
    """造一个结构合法的实例：base/name/.minecraft/saves"""
    gd = os.path.join(base, name, ".minecraft")
    os.makedirs(os.path.join(gd, "saves"), exist_ok=True)
    # is_game_dir 的判定依据：有 versions 或 saves
    open(os.path.join(gd, "options.txt"), "w").close()
    return Instance(name=name, root=os.path.join(base, name),
                    game_dir=gd, source="测试", game_version="1.20.1")


# ============================================================ A. 重扫持久化
class FakeCfg:
    """只实现 _merge_instances 需要的部分"""

    def __init__(self):
        self.data: dict = {}

    def get(self, k, d=None):
        return self.data.get(k, d)

    def set(self, k, v):
        self.data[k] = v


class Harness:
    """抽出被测逻辑，避免依赖 tkinter 起真实窗口"""

    def __init__(self, cfg: FakeCfg):
        self.cfg = cfg
        self.logs: list = []

    def log(self, m):
        self.logs.append(m)

    # 与 main_window._merge_instances 保持一致的复制，
    # 直接引用真实实现更可靠，见下方 run()
    pass


def run_merge():
    print("\n[A] 实例重扫：结果落盘 + 与缓存合并")

    # 直接取真实实现，避免测试与实现脱节
    import inspect
    import ui.main_window as mw
    src = inspect.getsource(mw.App._merge_instances)
    print(f"    （使用 main_window.App._merge_instances 真实实现）")

    base = os.path.join(TMP, "insts")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)

    old = mk_instance(base, "1.20.1-Forge")          # 缓存里已有的
    new = mk_instance(base, "1.12.2-Forge")          # 重扫新发现的

    class H:
        def __init__(self, cfg):
            self.cfg = cfg
            self.logs = []

        def log(self, m):
            self.logs.append(m)

    # 把真实方法挂上来
    H._merge_instances = mw.App._merge_instances

    cfg = FakeCfg()
    cfg.set("known_instances", [old.to_dict()])
    h = H(cfg)

    merged = h._merge_instances([new])
    names = sorted(i.name for i in merged)
    check("合并后同时含旧实例与新实例",
          names == ["1.12.2-Forge", "1.20.1-Forge"], str(names))
    check("合并结果数量为 2", len(merged) == 2, str(len(merged)))

    # 缓存里已失效（目录被删）的条目不应保留
    shutil.rmtree(old.root, ignore_errors=True)
    merged2 = h._merge_instances([new])
    names2 = sorted(i.name for i in merged2)
    check("失效路径被剔除", names2 == ["1.12.2-Forge"], str(names2))

    # 同路径重复不产生副本
    merged3 = h._merge_instances([new, mk_instance(base, "1.12.2-Forge")])
    check("同路径去重", len(merged3) == 1, str(len(merged3)))

    # ---- 关键：检查 done() 分支对 known 模式也写了缓存
    src_done = inspect.getsource(mw.App.scan_instances)
    check("known 模式也会持久化（persist=True）",
          "persist=True" in src_done and "merge = (mode == \"known\")" in src_done,
          "")
    check("known 模式也会置 scanned_once",
          'self.cfg.set("scanned_once", True)' in src_done, "")


# ============================================================ B. 下载归位
def run_place():
    print("\n[B] 下载归位：按整合包名自动匹配本机实例")

    import inspect
    import ui.main_window as mw

    base = os.path.join(TMP, "place")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)

    pack = mk_instance(base, "1.20.1-Forge")
    other = mk_instance(base, "1.12.2-OptiFine")

    class H:
        def __init__(self, instances):
            self.instances = instances
            self.cur_instance = other
            self.cloud = [{"name": "MyWorld", "display": "MyWorld",
                           "instance": "1.20.1-Forge"}]
            self.logs = []

        def log(self, m):
            self.logs.append(m)

        _cloud_record = mw.App._cloud_record
        _norm_inst = staticmethod(mw.App._norm_inst)
        _find_instance_by_name = mw.App._find_instance_by_name
        _resolve_download_target = mw.App._resolve_download_target

    h = H([pack, other])

    # 1) 名字完全一致 -> 命中
    m = h._find_instance_by_name("1.20.1-Forge")
    check("同名实例可被找到", m is not None and m.name == "1.20.1-Forge",
          str(m.name if m else None))

    # 2) 大小写 / 空格 / 括号差异也能匹配（多人环境命名不统一很常见）
    for variant in ["1.20.1-forge", "1.20.1 Forge", "1.20.1_Forge", "1.20.1Forge"]:
        check(f"归一化后可匹配「{variant}」",
              h._find_instance_by_name(variant) is not None, "")

    # 3) 不存在的包 -> 不误匹配
    check("不存在的整合包返回 None",
          h._find_instance_by_name("9.9.9-None") is None, "")

    # 4) 解析目标：命中同名实例且本地无此存档 -> 直接装过去
    tgt = h._resolve_download_target("MyWorld")
    expect = os.path.join(pack.saves_dir, "MyWorld")
    check("自动装到同名实例的 saves 目录", tgt == expect, f"{tgt}")

    # 5) 云端键带限定后缀时，用 display 还原真实存档名
    h.cloud = [{"name": "MyWorld (1.20.1-Forge)", "display": "MyWorld",
                "instance": "1.20.1-Forge"}]
    tgt2 = h._resolve_download_target("MyWorld (1.20.1-Forge)")
    check("带后缀的云端键还原为原始存档名",
          tgt2 == expect, f"{tgt2}")

    # 6) 本地已存在同名存档 -> 应弹窗让用户确认（返回 None 表示待对话框）
    os.makedirs(expect, exist_ok=True)
    open(os.path.join(expect, "level.dat"), "wb").close()
    tgt3 = h._resolve_download_target("MyWorld")
    check("本地已存在时交给用户选择（返回 None）", tgt3 is None, f"{tgt3}")

    # 7) 没有同名实例时，兜底到当前实例目录（再由对话框确认）
    h.cloud = [{"name": "W", "display": "W", "instance": "不存在的整合包"}]
    tgt4 = h._resolve_download_target("W")
    check("无同名实例时给出兜底路径",
          tgt4 is None or tgt4.endswith(os.path.join(other.saves_dir, "W")),
          f"{tgt4}")

    print("\n[C] 跨机器场景模拟")
    # A 机器：整合包「BetterMC」里有个存档 MyWorld
    # B 机器：也有整合包「BetterMC」，但本机还没这个存档
    b_base = os.path.join(TMP, "machineB")
    shutil.rmtree(b_base, ignore_errors=True)
    os.makedirs(b_base, exist_ok=True)
    b_pack = mk_instance(b_base, "BetterMC")

    class B:
        instances = [b_pack]
        cur_instance = b_pack
        cloud = [{"name": "MyWorld", "display": "MyWorld", "instance": "BetterMC"}]
        logs = []

        def log(self, m):
            self.logs.append(m)

        _cloud_record = mw.App._cloud_record
        _norm_inst = staticmethod(mw.App._norm_inst)
        _find_instance_by_name = mw.App._find_instance_by_name
        _resolve_download_target = mw.App._resolve_download_target

    b = B()
    got = b._resolve_download_target("MyWorld")
    want = os.path.join(b_pack.saves_dir, "MyWorld")
    check("B 下载 A 上传的存档 → 装到 B 的 BetterMC/saves/MyWorld",
          got == want, f"{got}")
    check("无需用户干预（自动归位）", got is not None, "")


def main():
    print("\n=== 重扫持久化 & 下载归位 回归测试 ===")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    run_merge()
    run_place()
    print("\n" + "=" * 58)
    bad = [n for n, ok_ in OK if not ok_]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过")
    print("=" * 58)


if __name__ == "__main__":
    main()
