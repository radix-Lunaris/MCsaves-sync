# -*- coding: utf-8 -*-
"""启动流程测试：缓存快速恢复 + 后台自动重扫已知目录 + 客户端直连"""
from __future__ import annotations
import os, sys, inspect, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
TMP = "/tmp/mcsync-boot"

import ui_smoke  # 安装 tkinter 桩件
ui_smoke.install_stubs()
import ui.main_window as mw
from core.models import Instance

OK = []
def check(n, c, e=""):
    print(("  ✅ " if c else "  ❌ ") + n + (f"  — {e}" if e else ""))
    OK.append((n, c))
    if not c: raise SystemExit(f"失败：{n} {e}")

def mk(base, name):
    gd = os.path.join(base, name, ".minecraft")
    os.makedirs(os.path.join(gd, "saves"), exist_ok=True)
    open(os.path.join(gd, "options.txt"), "w").close()
    return Instance(name=name, root=os.path.join(base, name), game_dir=gd, source="测试")

def main():
    print("\n=== 启动流程测试 ===")
    shutil.rmtree(TMP, ignore_errors=True); os.makedirs(TMP, exist_ok=True)
    base = os.path.join(TMP, "insts")
    a = mk(base, "1.20.1-Forge"); b = mk(base, "1.12.2-Forge")

    print("\n[1] 启动应为「缓存恢复 + 后台重扫」两段式")
    src = inspect.getsource(mw.App._boot)
    check("_boot 传入 then_rescan=True", "then_rescan=True" in src, "")
    src_scan = inspect.getsource(mw.App.scan_instances)
    # 用 AST 解析而不是字符串匹配：源码换行/格式化一变，字符串断言就误报
    import ast as _ast
    import textwrap as _tw
    _tree = _ast.parse(_tw.dedent(src_scan))
    _has_known = _has_bg = False
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Call) and getattr(_n.func, "attr", "") == "scan_instances":
            for _kw in _n.keywords:
                if _kw.arg == "mode" and getattr(_kw.value, "value", "") == "known":
                    _has_known = True
                if _kw.arg == "background" and getattr(_kw.value, "value", "") is True:
                    _has_bg = True
    check("quick 分支排了 known 重扫", _has_known, "")
    check("启动重扫走后台模式（不阻塞界面、不禁用按钮）", _has_bg, "")
    # 开关：允许用户彻底关闭启动重扫
    check("启动重扫受设置开关控制", "rescan_on_start" in src_scan, "")

    print("\n[2] 重扫结果与缓存合并（不丢已有实例）")
    class H:
        def __init__(self, cfg): self.cfg = cfg; self.logs=[]
        def log(self, m): self.logs.append(m)
    H._merge_instances = mw.App._merge_instances
    class C:
        def __init__(self): self.data={}
        def get(self,k,d=None): return self.data.get(k,d)
        def set(self,k,v): self.data[k]=v
    c = C(); c.set("known_instances", [a.to_dict()])
    h = H(c)
    merged = h._merge_instances([b])
    check("合并后含两个实例",
          sorted(i.name for i in merged) == ["1.12.2-Forge", "1.20.1-Forge"],
          str(sorted(i.name for i in merged)))

    print("\n[3] 客户端直连（环境变量里有代理也不受影响）")
    from core.api import proxy_settings
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:10808"
    t, px, note = proxy_settings("http://47.117.101.13:36854")
    check("决策为直连", t is False and px.get("http","") == "", note)

    print("\n" + "="*58)
    bad=[n for n,o in OK if not o]
    if bad: print(f"❌ {bad}"); raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过")
    print("="*58)

if __name__ == "__main__":
    main()
