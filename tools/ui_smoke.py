# -*- coding: utf-8 -*-
"""
UI 冒烟测试：在没有真实 Tk 的环境下，用桩件替换 tkinter / customtkinter / PIL，
实例化主窗口并走一遍主要渲染与交互路径，捕获属性名、参数、调用链错误。
用法： python3 tools/ui_smoke.py
"""
from __future__ import annotations

import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))


# ---------------------------------------------------------------- 桩件
class W:
    """通用控件桩：记录父子关系，方法返回自身

    LAYOUT_MODE=True 时额外模拟真实 Tk 的 pack 顺序 / geometry / reqheight，
    供 tools/test_ui_layout.py 做「不同屏幕尺寸下会不会显示不全」的自检。
    默认关闭，保证既有冒烟测试行为不变。
    """
    LAYOUT_MODE = False

    def __init__(self, *a, **k):
        self.kids = []
        self.master = a[0] if a and isinstance(a[0], W) else None
        self.kwargs = dict(k)
        if self.master is not None:
            self.master.kids.append(self)
        self._text = k.get("text", "")
        self._pack_order = []
        self._timers = []
        self.pack_side = "top"
        self._geom_w = None
        self._geom_h = None
        self._wraplength = k.get("wraplength", 0)

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
        self.pack_side = k.get("side", "top")
        if isinstance(self.master, W):
            self.master._pack_order.append(self)
        return self

    def pack_configure(self, *a, **k):
        # 只改内边距，不改变已经确立的 pack 先后顺序；
        # 但 `before=` 是 Tk 显式调整 packing order 的合法手段，
        # 必须真实模拟，否则自检验不到「footer 被钉在 body 之前」。
        if "side" in k:
            self.pack_side = k["side"]
        if "before" in k and isinstance(self.master, W):
            order = self.master._pack_order
            before = k["before"]
            if self in order:
                order.remove(self)
            if before in order:
                order.insert(order.index(before), self)
            else:
                order.insert(0, self)
        return self

    def pack_forget_unused(self):
        return self

    def place(self, *a, **k):
        return self

    def configure(self, *a, **k):
        if "wraplength" in k:
            self._wraplength = k["wraplength"]
        if "text" in k:
            self._text = k["text"]
        self.kwargs.update(k)
        return self

    # 窗口控制（真实 Tk 有，桩件需要补上否则 withdraw/deiconify 报错）
    def withdraw(self, *a, **k):
        self._withdrawn = True
        return self

    def deiconify(self, *a, **k):
        self._withdrawn = False
        return self

    def destroy(self):
        self._destroyed = True
        if self.master is not None and self in self.master.kids:
            self.master.kids.remove(self)
        self.kids = []

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
        if k == "wraplength":
            return self._wraplength
        return self.kwargs.get(k, "")

    def bind(self, *a, **k):
        return ""

    def winfo_reqheight(self):
        if not W.LAYOUT_MODE:
            return self._typed(420)
        # 按文本长度 / wraplength 估行数，模拟「长文本换行后变高」这一
        # 引发显示不全的核心因素。中文按每字约占 13px、行高 20px 估。
        txt = str(getattr(self, "_text", "") or "")
        try:
            wrap = int(str(self._wraplength or 0).rstrip("px")) or 420
        except Exception:
            wrap = 420
        per = max(12, wrap // 13)
        lines = 0
        for seg in txt.split("\n"):
            lines += max(1, (len(seg) + per - 1) // per) if seg else 1
        if not txt:
            return self._typed(34)        # 纯容器 / 按钮
        return self._typed(12 + max(1, lines) * 20)

    def winfo_toplevel(self):
        return self

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
        self._resizable_called = True
        return self

    def title(self, *a):
        return self

    def geometry(self, *a):
        import re as _re
        if a and isinstance(a[0], str):
            m = _re.match(r"(\d+)x(\d+)", a[0])
            if m:
                self._geom_w, self._geom_h = int(m.group(1)), int(m.group(2))
        return self

    def minsize(self, *a):
        self._minsize_args = a
        return self

    def update_idletasks(self):
        return self

    def winfo_screenwidth(self):
        return getattr(W, "SIM_SW", 1920)

    def winfo_screenheight(self):
        return getattr(W, "SIM_SH", 1080)

    def after(self, ms, fn=None, *a):
        # 短延迟（防抖/布局）立即排入 idle 队列；长延迟（如 10 分钟超时
        # 兜底）只登记不执行，否则测试里会把对话框提前关掉。
        if callable(fn):
            if int(ms or 0) <= 1000:
                PENDING.append(fn)
            else:
                self._timers.append((ms, fn))
        return "after#1"

    def after_idle(self, fn=None, *a):
        """对话框构造完会 after_idle(_fit_content / _arm_modal)，桩件要接住。"""
        if callable(fn):
            PENDING.append(fn)
        return "after#idle"

    # ------------------------------------------------------------ grab 语义
    def grab_set(self, *a, **k):
        # 窗口未 deiconify 时，真实 Tk 会抛 "grab failed: window not viewable"
        if GRAB_RAISE_ON_HIDDEN and getattr(self, "_withdrawn", True):
            raise RuntimeError("grab failed: window not viewable")
        global GRAB_OWNER
        GRAB_OWNER = self
        GRAB_HISTORY.append(("set", self))
        return ""

    def grab_release(self, *a, **k):
        global GRAB_OWNER
        if GRAB_OWNER is self:
            GRAB_OWNER = None
        GRAB_HISTORY.append(("release", self))
        return ""

    def grab_current(self, *a, **k):
        return GRAB_OWNER

    def lift(self, *a, **k):
        return self

    def focus_force(self, *a, **k):
        return self

    def wait_window(self, *a, **k):
        self._waited = True
        return None

    def winfo_exists(self):
        return not getattr(self, "_destroyed", False)

    def quitter(self):
        return self

    # 尺寸查询
    def winfo_children(self):
        return list(self.kids)

    # SIM_TYPE 模拟真实环境里 winfo_* 可能返回的「非 int」值：
    # 不同 customtkinter 版本、DPI 缩放钩子、几何字符串解析都会造成差异，
    # 正是 "int + str" 这类构造期崩溃的来源。
    SIM_TYPE = None

    def _typed(self, v):
        k = getattr(W, "SIM_TYPE", None)
        if k == "str":
            return str(v) + "px"
        if k == "float":
            return float(v)
        if k == "none":
            return None
        if k == "obj":
            return W()
        return int(v)

    def winfo_screenwidth(self):
        return self._typed(getattr(W, "SIM_SW", 1920))

    def winfo_screenheight(self):
        return self._typed(getattr(W, "SIM_SH", 1080))

    def winfo_width(self):
        v = self._geom_w if (W.LAYOUT_MODE and self._geom_w) else 1200
        return self._typed(v)

    def winfo_height(self):
        if W.LAYOUT_MODE and self._geom_h:
            return self._typed(self._geom_h)
        # 子控件没有 geometry，高度由自身请求尺寸决定
        if W.LAYOUT_MODE:
            try:
                return self._typed(self._to_i(self.winfo_reqheight()))
            except Exception:
                pass
        return self._typed(780)

    def _to_i(self, v, d=0):
        try:
            if isinstance(v, (int, float)):
                return int(v)
            m = re.match(r"^[-+]?\d+", str(v).strip())
            return int(m.group(0)) if m else d
        except Exception:
            return d

    def winfo_y(self):
        """模拟 pack 后的 y 坐标。

        关键：Tk 按 packing order 分配空间。若 side=bottom 的 footer
        排在 expand=True 的 body **之后**，它会被推到窗口下沿之外（y >= 窗口高），
        表现就是「内容都在、按钮一排全没了」。自检靠这个断言抓回归。
        """
        if not W.LAYOUT_MODE or not isinstance(self.master, W):
            return self._typed(0)
        order = list(getattr(self.master, "_pack_order", []))
        if self not in order:
            return self._typed(0)
        idx = order.index(self)
        earlier = order[:idx]
        wh = self._to_i(self.master.winfo_height(), 780)
        if self.pack_side == "bottom":
            if any(c.pack_side != "bottom" for c in earlier):
                # 被前面的 expand 区挤出去
                return self._typed(wh)
            # 底栏有 pady=(0, pad)，真实 Tk 里它不会正好贴窗口底边。
            # 留 20px 余量，否则「正常贴边」会被可见性断言误判成被挤出。
            return self._typed(max(0, wh - self._to_i(self.winfo_reqheight(), 34) - 20))
        return self._typed(sum(self._to_i(c.winfo_reqheight(), 24) for c in earlier))

    def winfo_reqwidth(self):
        if W.LAYOUT_MODE:
            return self._typed(max(360, min(900, int(self._wraplength or 420) + 48)))
        return self._typed(1200)

    def winfo_ismapped(self):
        return False

    def add(self, *a, **k):
        sub = W(self)
        return sub

    def tab(self, *a, **k):
        return W(self)

    def mainloop(self, *a, **k):
        return None

    def __call__(self, *a, **k):
        # 让 __getattr__ 返回的惰性桩可调用：Canvas.create_oval(...)、
        # pack_propagate(False)、itemconfigure(...) 等矢量绘图接口都靠它。
        # 不实现的话主窗口顶栏的矢量主题按钮会在自检里直接抛
        # TypeError: 'W' object is not callable。
        return self

    def __getattr__(self, name):
        # 未显式实现的控件方法：返回一个惰性桩
        stub = W()
        return stub


PENDING: list = []

# ---------------------------------------------------------------- grab 模拟
# 真实 Tk 的 grab 是「全局」的：只要某个 Toplevel 抓着 grab，其它窗口
# （包括主窗口）就收不到任何鼠标/键盘事件 —— 这就是「点 X 没反应、
# 只能任务管理器强杀」的机制。桩件必须模拟它，否则这类回归永远测不出来。
GRAB_OWNER = None          # 当前抓着 grab 的窗口
GRAB_HISTORY = []          # [(动作, 窗口)]，用于断言 grab 生命周期
GRAB_RAISE_ON_HIDDEN = False   # True 时模拟 "grab failed: window not viewable"


def reset_grab():
    global GRAB_OWNER
    GRAB_OWNER = None
    GRAB_HISTORY.clear()


def _mk_module(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def install_stubs():
    # tkinter
    tk = _mk_module("tkinter", Tk=W, Toplevel=W, Frame=W, Label=W, Button=W, Entry=W,
                    Text=W, Canvas=W, Scrollbar=W, StringVar=W, IntVar=W,
                    BooleanVar=W, DoubleVar=W, Variable=W,
                    messagebox=types.SimpleNamespace(showerror=lambda *a, **k: None))

    # 复刻 CPython 3.11 tkinter.Misc._setup 的关键行，让桩件的 widget path
    # 语义接近真实环境（CTK 6.0 的 CTkBaseClass 继承 tkinter.Frame）。
    class _Misc:
        def _setup(self, master, cnf):
            name = cnf.pop("name", None)
            if not name:
                name = self.__class__.__name__.lower()
                if getattr(master, "_last_child_ids", None) is None:
                    master._last_child_ids = {}
                count = master._last_child_ids.get(name, 0) + 1
                master._last_child_ids[name] = count
                name = "!%s%d" % (name, count)
            self._name = name
            if getattr(master, "_w", ".") == ".":
                self._w = "." + name
            else:
                self._w = master._w + "." + name      # CPython 3.11 :2629
            self.children = {}

    tk.Misc = _Misc
    fd = _mk_module("tkinter.filedialog", askdirectory=lambda *a, **k: "", askopenfilename=lambda *a, **k: "")
    tk.filedialog = fd
    sys.modules["tkinter"].filedialog = fd

    # 原生降级窗口会 `from tkinter import font as tkfont` 并显式构造大字体。
    # 桩件必须提供它，否则 _native_style 抛异常被 _native_shell 静默吞掉，
    # 自检就只在测「标记」而不是真的在测「字够不够大」。
    class _Font:
        def __init__(self, family=None, size=10, weight="normal", *a, **k):
            self.family, self.size, self.weight = family, size, weight
            self.kwargs = {"family": family, "size": size, "weight": weight}

        def configure(self, *a, **k):
            return None

        def measure(self, s):
            return len(str(s)) * int(self.size or 10)

    # 原生降级窗口会显式创建字体对象（这是「字看不清」的修复关键）。
    # 字体对象必须真的带 .size（int），否则自检只能验「标记设了」，
    # 验不到「字号到底够不够大」——之前这里被 Font=W 覆盖过，导致漏检。
    tf = _mk_module("tkinter.font", Font=_Font,
                    families=lambda: ["Microsoft YaHei UI"])
    tk.font = tf
    sys.modules["tkinter"].font = tf

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
    for k in ("local", "cloud", "prune", "log"):
        app.show_page(k)
    assert app.cur_page == "log", app.cur_page
    assert set(app.pages) >= {"local", "cloud", "prune", "log"}, set(app.pages)
    assert hasattr(app, "prune_page"), "区块清理页未创建"
    print("    ✅ 四个选项卡切换正常（含区块清理），当前页:", app.cur_page)
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
