# -*- coding: utf-8 -*-
# This file is part of MCSync.
# Copyright (C) 2025 radix-Lunaris
#
# MCSync is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# MCSync is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along
# with MCSync. If not, see <https://www.gnu.org/licenses/>.

"""
MCSync 客户端入口
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



def _excepthook_patch():
    # 语言必须在构建界面之前设定：界面文案在构建时就求值了
    try:
        from core.config import cfg
        from core.i18n import set_lang
        set_lang(str(cfg.get("language", "zh") or "zh"))
    except Exception:
        pass
    """打包成 --windowed 时也要能看到致命错误"""
    if getattr(sys, "frozen", False):
        import traceback

        def hook(exc_type, exc, tb):
            msg = "".join(traceback.format_exception(exc_type, exc, tb))
            try:
                import tkinter.messagebox as mb
                mb.showerror("MCSync 发生错误", msg[-1500:])
            except Exception:
                pass
            print(msg)

        sys.excepthook = hook


def _emergency_settings(err: Exception):
    """主界面起不来时的最后一道兜底：一个只依赖 tkinter 的最小窗口。

    用户最不能接受的情况是「旧服务器失效 → 双击没反应 → 连改地址的地方
    都没有」。这里保证无论主题、界面、登录流程出了什么问题，都至少有一个
    能改服务器地址 / 清登录状态的入口。刻意不用 customtkinter，这样即使
    崩的是它的初始化，本窗口依然能显示。
    """
    import json
    import tkinter as tk
    import tkinter.ttk as ttk
    from tkinter import messagebox

    # 直接读写配置文件，绕开任何可能已经损坏的模块
    try:
        from core.config import cfg
        cur_url = str(cfg.get("server_url", "") or "")

        def write(key, value):
            cfg.set(key, value)
    except Exception:
        import os
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "MCSync", "config.json")
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
        cur_url = str(data.get("server_url", "") or "")

        def write(key, value):
            data[key] = value
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    root = tk.Tk()
    root.title("MCSync · 紧急设置")
    # 旧版写死 520x300 + 不可调整：高 DPI / 大字号设备上底部按钮会被裁掉。
    # 现在先按屏幕可用空间定上限，内容填完后再按实际高度长高。
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(560, max(380, sw - 80))
    h = min(340, max(260, sh - 140))
    root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//3)}")
    root.minsize(min(380, w), min(240, h))
    root.resizable(True, True)

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="MCSync 主界面未能启动", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    lbl_err = ttk.Label(frm, text=f"原因：{err}", foreground="#a33")
    lbl_err.pack(anchor="w", pady=(6, 12))
    lbl_tip = ttk.Label(frm, text="如果旧服务器已失效，可在这里直接改成新地址，然后重新打开软件。")
    lbl_tip.pack(anchor="w", pady=(0, 10))

    ttk.Label(frm, text="服务器地址").pack(anchor="w")
    var = tk.StringVar(value=cur_url)
    ent = ttk.Entry(frm, textvariable=var, width=56)
    ent.pack(fill="x", pady=(2, 12))
    ent.focus_set()
    ent.select_range(0, "end")

    def save():
        v = var.get().strip().rstrip("/")
        if not v:
            messagebox.showwarning("MCSync", "请填写服务器地址", parent=root)
            return
        write("server_url", v)
        messagebox.showinfo("MCSync", f"已保存：{v}\n请重新打开 MCSync。", parent=root)
        root.destroy()

    def clear_login():
        for k in ("username", "paired_server", "client_id", "me_cache",
                  "logged_in", "remember_password"):
            write(k, "" if k != "remember_password" else False)
        try:
            from core.config import cfg as _c
            _c.set_secret("", "password")
            _c.set_secret("")
        except Exception:
            pass
        messagebox.showinfo("MCSync", "已清空本地登录状态，请重新打开软件登录。", parent=root)

    bar = ttk.Frame(root)
    bar.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
    ttk.Button(bar, text="保存地址并重启", command=save).pack(side="left")
    ttk.Button(bar, text="清空登录状态", command=clear_login).pack(side="left", padx=8)
    ttk.Button(bar, text="关闭", command=root.destroy).pack(side="right")

    def fit(_e=None):
        if _e is not None and _e.widget is not root:
            return
        try:
            ww = max(240, root.winfo_width() - 56)
            lbl_err.configure(wraplength=ww)
            lbl_tip.configure(wraplength=ww)
            root.update_idletasks()
            need = root.winfo_reqheight() + 12
            max_h = max(260, sh - 140)
            if need > root.winfo_height() and need <= max_h:
                root.geometry(f"{root.winfo_width()}x{need}")
        except Exception:
            pass

    root.bind("<Configure>", fit)
    root.after_idle(fit)
    root.mainloop()


def main():
    _excepthook_patch()
    try:
        from ui.main_window import main as _run
        _run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            _emergency_settings(e)
        except Exception:
            # 连兜底窗口都起不来时，至少留个弹窗告诉用户错误
            try:
                import tkinter.messagebox as mb
                mb.showerror("MCSync 启动失败", str(e))
            except Exception:
                pass


if __name__ == "__main__":
    main()
