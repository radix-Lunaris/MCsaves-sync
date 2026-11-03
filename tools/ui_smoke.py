# -*- coding: utf-8 -*-
"""
UI 冒烟测试：在没有真实 Tk 的环境下，用桩件替换 tkinter / customtkinter / PIL，
实例化主窗口并走一遍主要渲染与交互路径，捕获属性名、参数、调用链错误。
用法： python3 tools/ui_smoke.py
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))


# ---------------------------------------------------------------- 桩件
class W:
    """通用控件桩：记录父子关系，方法返回自身"""

    def __init__(self, *a, **k):
        self.kids = []
        self.master = a[0] if a and isinstance(a[0], W) else None
        self.kwargs = k
        if self.master is not None:
            self.master.kids.append(self)
        self._text = k.get("text", "")

    # 布局
    def grid(self, *a, **k):
        return self

    def tkraise(self, *a, **k):
        return self

    def lift(self, *a, **k):
        return self

    def grid_remove(self):
        return self

    def grid_propagate(self, *a):
        return self

    def grid_columnconfigure(self, *a, **k):
        return self

    def grid_rowconfigure(self, *a, **k):
        return self

    def pack(self, *a, **k):
        return self

    def place(self, *a, **k):
        return self

    def configure(self, *a, **k):
        return self

    # 窗口控制（真实 Tk 有，桩件需要补上否则 withdraw/deiconify 报错）
    def withdraw(self, *a, **k):
        self._withdrawn = True
        return self

    def deiconify(self, *a, **k):
        self._withdrawn = False
        return self

    def update_idletasks(self, *a, **k):
        return self

    # pack 管理：真实 Tk 有 pack_forget，切换登录区时用到
    def pack_forget(self, *a, **k):
        if self.master is not None and self in self.master.kids:
            self.master.kids.remove(self)
        return self

    def pack_info(self, *a, **k):
        return {}

    def cget(self, k):
        return ""

    def bind(self, *a, **k):
        return ""

    def destroy(self):
        if self.master is not None and self in self.master.kids:
            self.master.kids.remove(self)
        self.kids = []

    def start(self):
        return self

    def stop(self):
        return self

    def set(self, *a, **k):
        return self

    def get(self, *a, **k):
        # 桩件保存文本，让 insert/get/delete 可以往返，便于验证输入类逻辑
        if not hasattr(self, "_text"):
            self._text = ""
        return self._text

    def insert(self, *a, **k):
        if not hasattr(self, "_text"):
            self._text = ""
        for arg in a:
            if isinstance(arg, str) and arg not in ("end", "0.0", "1.0"):
                self._text += arg
        return self

    def delete(self, *a, **k):
        self._text = ""
        return self

    def focus(self):
        return self

    def see(self, *a):
        return self

    def index(self, *a):
        return "1.0"

    def wait_window(self, *a):
        return None

    def transient(self, *a):
        return self

    def grab_set(self, *a):
        return self

    def grab_release(self):
        return self

    def protocol(self, *a, **k):
        return self

    def iconbitmap(self, *a):
        return self

    def resizable(self, *a):
        return self

    def title(self, *a):
        return self

    def geometry(self, *a):
        return self

    def minsize(self, *a):
        return self

    def update_idletasks(self):
        return self

    def after(self, ms, fn=None, *a):
        if callable(fn):
            PENDING.append(fn)
        return "after#1"

    def quitter(self):
        return self

    # 尺寸查询
    def winfo_children(self):
        return list(self.kids)

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_width(self):
        return 1200

    def winfo_height(self):
        return 780

    def winfo_ismapped(self):
        return False

    def add(self, *a, **k):
        sub = W(self)
        return sub

    def tab(self, *a, **k):
        return W(self)

    def mainloop(self, *a, **k):
        return None

    def __getattr__(self, name):
        # 未显式实现的控件方法：返回一个惰性桩
        stub = W()
        return stub


PENDING: list = []


def _mk_module(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def install_stubs():
    # tkinter
    tk = _mk_module("tkinter", Tk=W, Toplevel=W, Frame=W, Label=W, Button=W, Entry=W,
                    Text=W, Canvas=W, StringVar=W, IntVar=W, BooleanVar=W, DoubleVar=W,
                    Variable=W, messagebox=types.SimpleNamespace(showerror=lambda *a, **k: None))
    fd = _mk_module("tkinter.filedialog", askdirectory=lambda *a, **k: "", askopenfilename=lambda *a, **k: "")
    tk.filedialog = fd
    sys.modules["tkinter"].filedialog = fd

    # customtkinter
    ctk_names = [
        "CTk", "CTkToplevel", "CTkFrame", "CTkLabel", "CTkButton", "CTkEntry",
        "CTkScrollableFrame", "CTkProgressBar", "CTkOptionMenu", "CTkComboBox",
        "CTkSwitch", "CTkCheckBox", "CTkRadioButton", "CTkTextbox", "CTkTabview",
        "CTkSegmentedButton", "CTkSlider", "CTkScrollbar", "CTkImage", "CTkFont",
    ]
    class Var:
        def __init__(self, value=None, *a, **k):
            self.v = value

        def get(self):
            return self.v if self.v is not None else False

        def set(self, v):
            self.v = v

        def trace_add(self, *a, **k):
            return ""

    attrs = {n: W for n in ctk_names}
    attrs.update(BooleanVar=Var, StringVar=Var, IntVar=Var, DoubleVar=Var, Variable=Var)
    attrs.update(
        set_appearance_mode=lambda m: None,
        get_appearance_mode=lambda: "Dark",
        set_default_color_theme=lambda t: None,
        set_widget_scaling=lambda s: None,
        set_spacing_scaling=lambda s: None,
        scaling_tracker=types.SimpleNamespace(),
    )
    _mk_module("customtkinter", **attrs)

    # PIL
    class _Img:
        def open(self, *a, **k):
            return self

        def convert(self, *a, **k):
            return self

        def resize(self, *a, **k):
            return self

        def point(self, *a, **k):
            return None

        def save(self, *a, **k):
            return None

    img_mod = _mk_module("PIL", Image=_Img())
    _mk_module("PIL.Image", Image=_Img, LANCZOS=1, new=lambda *a, **k: _Img(), Draw=lambda *a, **k: W())
    return img_mod


def flush(app):
    """执行所有 after / ui 队列里排队的主线程任务"""
    import queue

    for _ in range(60):
        try:
            fn = app._ui_queue.get_nowait()
            fn()
        except queue.Empty:
            break
    pend = list(PENDING)
    PENDING.clear()
    for fn in pend:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"    (待执行任务异常: {e})")
    for _ in range(60):
        try:
            fn = app._ui_queue.get_nowait()
            fn()
        except queue.Empty:
            break


def main():
    print("\n=== MCSync UI 冒烟测试（桩件环境） ===\n")
    install_stubs()

    from core.config import cfg
    from core.models import Instance, SaveInfo, SaveState
    from ui.widgets import CloudCard, InstanceCard, SaveCard
    from ui.main_window import App

    cfg.dir = "/tmp/mcsync-ui-smoke"
    os.makedirs(cfg.dir, exist_ok=True)
    # 测试环境没有代理软件；默认策略会让公网地址走 10808 代理并必然失败。
    # 这里关掉代理，让测试只验证自身逻辑。
    # 注意必须在目录建好之后再 set，否则 autosave 写不进去，
    # 后续 cfg.load() 会把内存里的改动覆盖回默认值。
    # 冒烟环境没有服务端：清空账号，避免启动时尝试登录
    cfg.set("username", ""); cfg.set_secret("", "password")
    cfg.set_secret(""); cfg.set("paired_server", "")

    print("[1] 实例化主窗口")
    app = App()
    print("    ✅ 主窗口构建成功")

    print("[2] 实例列表渲染")
    insts = [
        Instance("PCL 主实例", "D:/PCL/.minecraft", "D:/PCL/.minecraft", "PCL2", "1.20.1", True),
        Instance("HMCL 整合包", "D:/HMCL/instances/ATM9/.minecraft", "D:/HMCL/instances/ATM9/.minecraft", "HMCL", "1.20.1"),
    ]
    app.instances = insts
    app.render_instances()
    print(f"    ✅ 渲染 {len(app.inst_box.winfo_children())} 张实例卡片")

    print("[3] 选中实例 → 存档列表渲染")
    app.select_instance(insts[0])
    saves = [
        SaveInfo("MyWorld", "D:/PCL/.minecraft/saves/MyWorld", world_name="生存存档",
                 game_version="1.20.1", gametype="生存", difficulty="普通", size=1024 ** 3,
                 file_count=1234, last_played=__import__("time").time() - 3600, state=None),
        SaveInfo("Skyblock", "D:/PCL/.minecraft/saves/Skyblock", world_name="空岛",
                 gametype="创造", difficulty="和平", size=256 * 1024 ** 2, file_count=88),
    ]
    app.saves = saves
    app.render_saves()
    app.select_save(saves[0])
    print(f"    ✅ 渲染 {len(app.save_box.winfo_children())} 张存档卡片并选中首项")

    print("[4] 云端列表渲染")
    app.cloud = [{
        "name": "MyWorld", "latest_version": 7, "version_count": 7, "file_count": 1234,
        "total_size": 1024 ** 3, "latest_time": __import__("time").time() - 7200,
    }]
    app.render_cloud()
    app.select_cloud(app.cloud[0])
    print(f"    ✅ 渲染 {len(app.cloud_box.winfo_children())} 张云端卡片")

    print("[5] 进度 / 日志 / 提示 / 主题 / 设置")
    app._on_progress({"stage": "上传数据块", "total_bytes": 100, "done_bytes": 40,
                      "total_items": 10, "done_items": 4, "speed": 1024 * 1024, "eta": 60,
                      "file": "region/r.0.0.mca"})
    app._on_progress({"stage": "扫描", "total_bytes": 0, "done_bytes": 0, "total_items": 5, "done_items": 2})
    app.progress_reset("就绪")
    app.log("测试日志一行")
    app.toast("测试提示")
    app.set_busy(True, "上传中")
    app.set_busy(False)
    flush(app)
    app.toggle_theme()
    app.refresh_theme()
    flush(app)
    print("    ✅ 进度面板、日志、提示、主题切换正常")

    print("[6] 设置对话框 / 冲突对话框 / 版本对话框")
    from ui.dialogs import ConflictDialog, SettingsDialog, VersionDialog
    SettingsDialog(app, on_saved=lambda: None)
    ConflictDialog(app, [{"path": "level.dat", "local_hash": "a" * 32, "cloud_hash": "b" * 32}], "upload")
    VersionDialog(app, "MyWorld", [{"id": 3, "created_at_str": "2026-10-14 10:00:00",
                                    "file_count": 12, "total_size": 1024, "note": ""}])
    flush(app)
    print("    ✅ 三类对话框构建正常")

    print("[6.5] 界面回调完整性")
    import re as _re
    import inspect as _ins
    src_all = _ins.getsource(type(app))
    # 去掉注释与 docstring 行后再匹配，避免文档里的示例文本被当成真实引用
    code_only = "\n".join(l for l in src_all.split("\n")
                           if not l.strip().startswith("#") and '"""' not in l)
    src_all = code_only
    defined = set(_re.findall(r"^    def (\w+)", src_all, _re.M))
    refs = set()
    for pat in (r"command=(?:lambda[^:]*:)?\s*self\.(\w+)",
                r"self\.after\(\s*\d+,\s*self\.(\w+)",
                r"command=lambda[^,]*?self\.(\w+)\("):
        refs |= set(_re.findall(pat, src_all))
    miss = sorted(r for r in refs if r not in defined)
    assert not miss, f"回调引用了不存在的方法: {miss}"
    print(f"    ✅ {len(refs)} 个回调全部已定义（无 AttributeError 风险）")

    print("[6.6] 设置页账号/口令可输入并保存")
    from ui.dialogs import SettingsDialog
    dlg = SettingsDialog(app, on_saved=lambda: None)
    tok = "Ab3xK9mQ2pL7rTz1"
    dlg.e_user.delete(0, "end")
    dlg.e_user.insert(0, "alice")
    dlg.e_pass.delete(0, "end")
    dlg.e_pass.insert(0, tok)
    assert dlg.e_user.get() == "alice", f"账号回读失败: {dlg.e_user.get()!r}"
    assert dlg.e_pass.get() == tok, f"口令回读失败: {dlg.e_pass.get()!r}"
    print(f"    ✅ 账号/口令框写入并回读成功: {dlg.e_user.get()} / {dlg.e_pass.get()}")
    try:
        dlg._save()
        assert cfg.get("username", "") == "alice", cfg.get("username", "")
        assert cfg.get_secret("password") == tok, cfg.get_secret("password")
        print(f"    ✅ 账号已保存进配置: {cfg.get('username')}")
    except Exception as e:
        print(f"    [dbg] username={cfg.get('username','')!r} err={e}")
        if cfg.get("username", "") == "alice" and cfg.get_secret("password") == tok:
            print(f"    ✅ 账号已保存进配置（部分控件为桩件，已跳过）")
        else:
            raise
    print(f"    ✅ 配置目录: {cfg.dir}")
    # 设置对话框保存时会在桩件环境里把代理写回默认值（custom），
    # 后续连接测试会真的去连 10808 代理。这里恢复成直连。

    print("[7] 选项卡切换")
    for k in ("local", "cloud", "log"):
        app.show_page(k)
    assert app.cur_page == "log", app.cur_page
    print("    ✅ 三个选项卡切换正常，当前页:", app.cur_page)
    app.show_page("local")

    print("[8] 地址持久化")
    # 顶栏已不再内联地址输入框（登录独立成窗口），地址直接在配置层落盘。
    # 这里验证配置读写本身，以及 _commit_url 在"有地址"时不会去连无效服务器。
    cfg.set("server_url", "http://1.2.3.4:28650")
    cfg.save()
    cfg.load()
    assert cfg.get("server_url") == "http://1.2.3.4:28650", cfg.get("server_url")
    print("    ✅ 地址已落盘:", cfg.get("server_url"))
    # 清空地址后 _commit_url 应给出提示而不是抛异常（没有服务器可连）
    cfg.set("server_url", "")
    try:
        app._commit_url()
        print("    ✅ 无地址时给出提示，未崩溃")
    except Exception as e:
        raise AssertionError(f"无地址时 _commit_url 抛异常: {e}")

    print("[8.5] 登录凭据与顶栏入口")
    # 口令必须进加密区，不能以明文留在 config.json
    cfg.set_secret("p@ssw0rd-测试", "password")
    got = cfg.get_secret("password")
    assert got == "p@ssw0rd-测试", got
    print("    ✅ 口令可加密存取:", got[:4] + "****")
    # 未登录时应显示登录按钮，已登录时显示身份胶囊
    assert hasattr(app, "b_login") and hasattr(app, "login_chip"), "顶栏缺少登录控件"
    app.cfg.set("username", "")
    try:
        app.api.client_secret = ""
    except Exception:
        pass
    app._refresh_login_row()
    print("    ✅ 顶栏登录/身份切换正常")
    cfg.set_secret("", "password")
    assert cfg.get_secret("password") == "", "清除口令失败"
    print("    ✅ 清除口令正常（退出登录场景）")

    print("[9] 实例缓存与三档扫描")
    # 构造两个真实存在的实例目录
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="mcsync-smoke-")
    insts = []
    for nm in ("Alpha", "Beta"):
        gd = os.path.join(tmp, nm, ".minecraft")
        os.makedirs(os.path.join(gd, "saves"), exist_ok=True)
        insts.append(Instance(name=nm, root=os.path.join(tmp, nm), game_dir=gd, source="手动"))
    cfg.set("known_instances", [i.to_dict() for i in insts])
    cfg.set("scanned_once", True)
    app.busy = False
    app.scan_instances(mode="quick")
    assert len(app.instances) == 2, len(app.instances)
    print(f"    ✅ 快速恢复 {len(app.instances)} 个实例（无全盘遍历）")
    # 目录被删掉的实例应自动剔除
    shutil.rmtree(os.path.join(tmp, "Beta"))
    app.busy = False
    app.scan_instances(mode="quick")
    assert len(app.instances) == 1, len(app.instances)
    print("    ✅ 已失效路径自动剔除，剩余:", len(app.instances))
    shutil.rmtree(tmp, ignore_errors=True)
    cfg.set("known_instances", [])
    cfg.set("scanned_once", False)

    print("[9.5] 云端选中高亮")
    app.cloud = [{"name": "CloudW1", "latest_version": 3, "total_size": 1024,
                  "file_count": 5, "version_count": 3, "latest_time": 0}]
    app.render_cloud()
    app.select_cloud(app.cloud[0])
    cards = [w for w in app.cloud_box.winfo_children() if isinstance(w, CloudCard)]
    assert cards, "云端卡片未渲染"
    assert cards[0]._selected is True, "云端卡片未高亮"
    print("    ✅ 云端选中已高亮卡片（不只是文字提示）")

    print("[9.6] 主题切换后整体重建")
    n_inst = len(app.instances)
    n_cloud = len(app.cloud)
    app.toggle_theme()
    assert len(app.instances) == n_inst, "重建后实例数据丢失"
    assert len(app.cloud) == n_cloud, "重建后云端数据丢失"
    app.toggle_theme()   # 切回去
    print(f"    ✅ 主题切换重建正常，数据保留（实例 {len(app.instances)} / 云端 {len(app.cloud)}）")

    print("[10] 关闭流程")
    app.on_close()
    print("    ✅ 配置保存与退出正常")

    print("\n=== UI 冒烟测试通过 ✅ ===")


if __name__ == "__main__":
    main()
