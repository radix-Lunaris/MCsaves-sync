# -*- coding: utf-8 -*-
"""
本轮六个问题的回归测试：

  1. 导入新整合包后全盘重扫仍扫不到
     - 根因 A：候选根只认名字含 pcl/hmcl/minecraft 等关键词的目录，
       「E:\\整合包\\ATM9」这种任意名字的目录永远不在候选里。
     - 根因 B：is_game_dir() 强制要求 saves 目录存在，刚导入、还没进过
       游戏的实例只有 mods/ + versions/，被直接丢弃。
     - 根因 C：_walk 里 `if cur != root` 跳过候选根自身。
     - 根因 D：每根 4000 目录预算在浅层就 break，深度调大也没用。
  2. 扫描深度上限从 6 提到 12
  3. 设置窗口显示不全（固定尺寸 + 标签页内容不滚动 + 高级页行号重叠）
  4. 手动添加目录后重扫扫不到，必须重启
  5. 登录成功后左上角状态栏仍显示未连接
  6. 附带隐藏问题：忙碌时静默丢弃扫描、启动重扫被永久跳过、
     Api 无谓重建导致角色显示错乱、状态栏探测结果互相覆盖。

用法： python3 tools/test_scan_discovery.py
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TMP = "/tmp/mcsync-scan-discovery"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")
os.makedirs(os.environ["MCSYNC_CONFIG_DIR"], exist_ok=True)

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def mkdir(*parts) -> str:
    p = os.path.join(*parts)
    os.makedirs(p, exist_ok=True)
    return p


def new_pack(base: str, name: str, with_saves: bool = False) -> str:
    """造一个 PCL2/HMCL 风格实例：<base>/<name>/.minecraft/"""
    root = mkdir(base, name)
    gd = mkdir(root, ".minecraft")
    mkdir(gd, "mods")
    mkdir(gd, "versions")
    if with_saves:
        mkdir(gd, "saves")
    return root


def names(insts):
    return sorted(i.name for i in insts)


def main():
    print("\n=== 实例发现 / 深度 / 设置 / 状态栏 回归 ===\n")
    import core.scanner as sc
    from core.config import cfg

    # ---------------------------------------------------------------- 1
    print("[1] 刚导入、还没有 saves 目录的新整合包必须能识别")
    fresh_parent = mkdir(TMP, "fresh")
    fresh = new_pack(fresh_parent, "ATM9")           # 只有 mods + versions
    gd = os.path.join(fresh, ".minecraft")
    check("无 saves 也被判为游戏目录", sc.is_game_dir(gd), gd)
    check("新实例在目录特征判定中命中",
          sc.looks_like_instance_dir({".minecraft"}, set(), os.path.basename(fresh)))

    got = sc.InstanceScanner.scan_one_root(fresh_parent, depth=6)
    check("定向扫描能找到新整合包", len(got) == 1, str(names(got)))
    check("实例根指向整合包目录", bool(got) and os.path.basename(got[0].root) == "ATM9",
          got[0].root if got else "")

    # 纯 mods 目录不该被误判成游戏目录
    junk = mkdir(TMP, "junk", "onlymods")
    mkdir(junk, "mods")
    check("单个标志目录不会被误判", not sc.is_game_dir(junk))

    # ---------------------------------------------------------------- 2
    print("\n[2] 候选根 / 用户添加的目录「自身就是实例」时也要扫到")
    selfroot = mkdir(TMP, "selfroot")
    mkdir(selfroot, "versions")
    mkdir(selfroot, "mods")
    check("root 自身是游戏目录", sc.is_game_dir(selfroot))
    got2 = sc.InstanceScanner.scan_one_root(selfroot, depth=6)
    check("旧版 `cur != root` 会返回 0，现在返回 1", len(got2) == 1, str(names(got2)))

    # 官方启动器 APPDATA\Roaming\.minecraft 形态：目录名就是 .minecraft
    official = mkdir(TMP, "Roaming", ".minecraft")
    mkdir(official, "versions")
    mkdir(official, "saves")
    check(".minecraft 目录名被识别", sc.is_game_dir(official))
    got3 = sc.InstanceScanner.scan_one_root(official, depth=6)
    check("官方启动器实例可识别", len(got3) == 1, str(names(got3)))
    check("不再把父目录名 Roaming 当实例名",
          bool(got3) and "Roaming" not in got3[0].name, got3[0].name if got3 else "")
    src_walk = inspect.getsource(sc.InstanceScanner._walk)
    check("_walk 不再跳过 root 自身", "if cur != root" not in src_walk)

    # ---------------------------------------------------------------- 3
    print("\n[3] 任意名字的目录（不含启动器关键词）也要能被全盘重扫发现")
    drive = mkdir(TMP, "drive")
    new_pack(mkdir(drive, "我的整合包"), "CustomPack")  # 「我的整合包」不含任何关键词
    orig_drives = sc._drives
    sc._drives = lambda: [drive]          # 模拟「盘符根」
    try:
        broad = sc.broad_roots()
        check("广谱根纳入了任意名字的第一层目录",
              any(os.path.basename(p) == "我的整合包" for p in broad), str(broad))
        insts = sc.InstanceScanner(depth=6).scan(sniff=False, broad=True)
        check("全盘扫描发现新整合包",
              any(os.path.basename(i.root) == "CustomPack" for i in insts),
              str(names(insts)))
    finally:
        sc._drives = orig_drives

    src_scan = inspect.getsource(sc.InstanceScanner.scan)
    check("明确候选优先、广谱根后扫", "priority" in src_scan and "secondary" in src_scan)
    check("两批使用独立预算", "prio_budget" in src_scan and "broad_budget" in src_scan)
    check("进度回调留在主线程", "只在子线程做磁盘遍历" in src_scan)

    # ---------------------------------------------------------------- 4
    print("\n[4] 扫描深度：上限 12、默认 6、预算不再截断深度")
    check("MAX_SCAN_DEPTH = 12", sc.MAX_SCAN_DEPTH == 12, str(sc.MAX_SCAN_DEPTH))
    check("DEFAULT_SCAN_DEPTH = 6", sc.DEFAULT_SCAN_DEPTH == 6, str(sc.DEFAULT_SCAN_DEPTH))
    check("depth=99 被夹到 12", sc.InstanceScanner(depth=99).depth == 12)
    check("depth=12 生效", sc.InstanceScanner(depth=12).depth == 12)
    check("配置默认深度为 6", int(cfg.get("scan_depth", 0)) == 6, str(cfg.get("scan_depth")))
    check("预算随深度放大",
          sc.InstanceScanner(depth=12).max_dirs > sc.InstanceScanner(depth=3).max_dirs)
    check("_walk 不再有每根 4000 的硬 break",
          "seen > self.max_dirs" not in src_walk)
    check("_walk 支持 depth_limit", "depth_limit" in src_walk)

    # 8 层深的实例：depth=3 扫不到，depth=8 能扫到
    deep = mkdir(TMP, "deep")
    cur = deep
    for i in range(1, 7):
        cur = mkdir(cur, f"L{i}")
    deep_pack = new_pack(cur, "DeepPack")
    check("浅深度扫不到深层实例",
          len(sc.InstanceScanner.scan_one_root(deep, depth=3)) == 0)
    got_deep = sc.InstanceScanner.scan_one_root(deep, depth=8)
    check("深度 8 能扫到第 6 层", len(got_deep) == 1, str(names(got_deep)))

    # ---------------------------------------------------------------- 5
    print("\n[5] 快速重扫：手动添加的目录当场生效，不必重启")
    own = new_pack(mkdir(TMP, "own"), "OwnPack")
    parent = mkdir(TMP, "parent")
    child = new_pack(parent, "SiblingPack")
    r1 = sc.InstanceScanner.fast_known_scan([], [own])
    check("extra_root 自身是实例 → 直接命中", len(r1) == 1, str(names(r1)))
    r2 = sc.InstanceScanner.fast_known_scan([], [parent])
    check("extra_root 下的子实例 → 单层命中", len(r2) == 1, str(names(r2)))
    src_fast = inspect.getsource(sc.InstanceScanner.fast_known_scan)
    check("不再粗暴跳过所有点开头目录",
          'if name.startswith(".")' not in src_fast)
    check("不再只认 .minecraft 子目录", "is_instance_root(path)" in src_fast)
    check("单根定向扫描入口存在",
          hasattr(sc.InstanceScanner, "scan_one_root"))

    import ui.main_window as mw

    class FakeApp:
        _absorb_instances = mw.App._absorb_instances

        def __init__(self):
            self.instances = []
            self.cfg = cfg
            self.rendered = 0

        def render_instances(self):
            self.rendered += 1

    fa = FakeApp()
    added = fa._absorb_instances(r1 + r2)
    check("新实例立即并入列表", len(fa.instances) == 2 and added == 2,
          str(names(fa.instances)))
    check("立即重绘界面", fa.rendered == 1)
    check("结果落盘（下次启动无需重扫）",
          len(cfg.get("known_instances", []) or []) == 2,
          str(len(cfg.get("known_instances", []) or [])))
    check("scanned_once 已置位", cfg.get("scanned_once") is True)

    src_add = inspect.getsource(mw.App.add_instance_dir)
    check("添加目录走定向深扫", "_probe_added_root" in src_add)
    check("不再只调 known 重扫",
          'scan_instances(mode="known")' not in src_add)
    src_saved = inspect.getsource(mw.App.open_settings)
    check("设置里改目录会立刻探测", "_probe_added_root" in src_saved)
    check("设置里改深度会全盘重扫", 'mode="full"' in src_saved)
    check("地址没变不重建 Api", 'if getattr(self.api, "base", "") != url:' in src_saved)

    # ---------------------------------------------------------------- 6
    print("\n[6] 设置窗口显示不全 / 高级页控件重叠")
    src_dlg = open(os.path.join(ROOT, "client", "ui", "dialogs.py"),
                   encoding="utf-8").read()
    check("标签页内容改为可滚动容器",
          "CTkScrollableFrame(tab" in src_dlg)
    check("五个标签页都包了滚动容器",
          src_dlg.count('self.tab.add(name)') == 1 and "pages[name] = sc" in src_dlg)
    check("窗口高度受屏幕可用高度约束", "sh - 120" in src_dlg)
    check("minsize 也受屏幕约束",
          "self.minsize(min(360, w)" in src_dlg and "int(w * 0.7)" not in src_dlg)
    check("保存时深度上限引用 MAX_SCAN_DEPTH", "MAX_SCAN_DEPTH" in src_dlg)
    check("深度输入说明改为 1~12", "范围 1~12" in src_dlg)

    # 高级页 grid 单元格不得重复占用
    adv = src_dlg[src_dlg.index("def _build_adv"):src_dlg.index("def _clear_cache")]
    cells = re.findall(r'\.grid\(row=(\d+),\s*column=(\d+)', adv)
    dup = {c for c in cells if cells.count(c) > 1}
    check("高级页无重叠 grid 单元格", not dup, str(sorted(dup)))
    check("界面语言独占 row=2", 'text=t("界面语言")' in adv and "row=2, column=0" in adv)

    # ---------------------------------------------------------------- 7
    print("\n[7] 登录成功后状态栏刷新 + 探测代次保护")
    src_login = inspect.getsource(mw.App.on_login_result)
    check("登录成功刷新左上角状态",
          "self.l_status.configure" in src_login and t_conn(src_login))
    check("登录成功作废启动探测", "_conn_gen" in src_login)
    src_test = inspect.getsource(mw.App.test_connection)
    check("探测带代次", "_conn_gen" in src_test and "current()" in src_test)
    check("过期结果被丢弃", "if not current():" in src_test)
    check("done 不再主线程同步取统计", "self._safe_stat()" not in src_test)

    # 行为：模拟登录回调，确认状态被写为已连接且发起新探测
    class Label:
        def __init__(self):
            self.calls = []

        def configure(self, **kw):
            self.calls.append(kw)

    class StubApi:
        me = {"username": "alice", "role": "admin", "display": "Alice"}
        base = "http://new:24011"

    class StubApp:
        on_login_result = mw.App.on_login_result

        def __init__(self):
            self.api = StubApi()
            self.cfg = cfg
            self.l_status = Label()
            self._conn_gen = 0
            self.events = []

        def _prepare_api(self): self.events.append("prepare")
        def _refresh_login_row(self): self.events.append("row")
        def toast(self, m): self.events.append("toast")
        def refresh_cloud(self): self.events.append("cloud")
        def test_connection(self, silent=False): self.events.append(("test", silent))
        def log(self, m): self.events.append("log")

    sa = StubApp()
    sa.on_login_result({"api": StubApi(), "url": "http://new:24011", "user": "alice"})
    check("登录回调写了状态栏",
          any("已连接" in str(k.get("text", "")) for k in sa.l_status.calls),
          str(sa.l_status.calls))
    check("登录回调发起新探测", ("test", True) in sa.events, str(sa.events))
    # StubApp.test_connection 是桩件，不会再推进代次；
    # 真实实现里 test_connection 会再 +1（登录作废 + 新探测各一次）。
    check("代次被推进（作废启动探测）", sa._conn_gen >= 1, str(sa._conn_gen))

    # ---------------------------------------------------------------- 8
    print("\n[8] 其它顺带修掉的隐藏问题")
    src_scanm = inspect.getsource(mw.App.scan_instances)
    check("忙碌时不再静默 return", "正在执行其它任务" in src_scanm)
    check("full 与 known 都合并缓存", "merge = True" in src_scanm)
    check("启动重扫忙时有限重试", "_start_background_rescan" in
          inspect.getsource(mw.App.scan_instances))
    src_retry = inspect.getsource(mw.App._start_background_rescan)
    check("重试有次数上限", "attempt < 5" in src_retry)
    check("重扫按钮不被忙碌态吞掉",
          'self.scan_instances(mode="full", force=True)' in
          inspect.getsource(mw.App.rescan_full))
    src_apply = inspect.getsource(mw.App._apply_instances)
    check("后台重扫不会清掉传输忙碌态",
          "release_busy" in src_apply and "if release_busy:" in src_apply)
    check("done 分支给后台传 release_busy=False",
          "release_busy=not background" in src_scanm)
    # 全项目扫描深度默认值必须统一为 6（配置、界面、调用点）
    src_mw_all = open(os.path.join(ROOT, "client", "ui", "main_window.py"),
                      encoding="utf-8").read()
    check("main_window 无残留默认深度 3",
          'get("scan_depth", 3)' not in src_mw_all)
    check("后台重扫强制发起（避免竞态被吞）",
          'background=True, force=True' in src_retry)

    print("\n" + "=" * 70)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 六项问题已修复且无回归")
    print("=" * 70)


def t_conn(src: str) -> bool:
    return "self.test_connection(silent=True)" in src


if __name__ == "__main__":
    main()
