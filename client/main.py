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


def main():
    _excepthook_patch()
    from ui.main_window import main as _run
    _run()


if __name__ == "__main__":
    main()
