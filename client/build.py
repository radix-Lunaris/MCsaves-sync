# -*- coding: utf-8 -*-
"""
MCSync Windows 客户端构建脚本

真正的构建逻辑都在这里, build.bat 只是个三行启动器。
这样做的原因: Windows 批处理对中文、括号、百分号的处理极其脆弱,
之前遇到的 "空白卡死" / "... was unexpected at this time" /
"'的程序' is not recognized" 全部是 cmd 解析层的坑, 换成 Python 后彻底消失。

用法:
    build.bat                单文件 exe -> dist\\MCSync.exe
    build.bat --dir          文件夹模式 -> dist\\MCSync\\MCSync.exe
    build.bat --no-venv      不建虚拟环境
    build.bat --console      保留控制台窗口
    build.bat --help         帮助
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAME = "MCSync"
ENTRY = os.path.join(HERE, "main.py")
VENV = os.path.join(HERE, ".buildenv")
ICON = os.path.join(HERE, "app.ico")


def ok(msg: str = "") -> None:
    print(f"  [OK] {msg}" if msg else "")


def info(msg: str = "") -> None:
    print(f"  [*] {msg}" if msg else "")


def warn(msg: str = "") -> None:
    print(f"  [!] {msg}" if msg else "")


def err(msg: str = "") -> None:
    print(f"  [X] {msg}" if msg else "", file=sys.stderr)


def hr(title: str = "") -> None:
    print()
    print("  " + "=" * 56)
    if title:
        print(f"   {title}")
        print("  " + "=" * 56)
    print()


def run(cmd: list, cwd: str | None = None, quiet: bool = False) -> int:
    """执行命令并实时输出; 返回退出码"""
    if not quiet:
        print(f"      $ {' '.join(str(c) for c in cmd)}")
    try:
        p = subprocess.run(cmd, cwd=cwd or HERE)
        return p.returncode
    except FileNotFoundError:
        return 127
    except Exception as e:  # noqa: BLE001
        err(f"命令执行异常: {e}")
        return 1


def venv_python() -> str:
    p = os.path.join(VENV, "Scripts", "python.exe")
    return p if os.path.exists(p) else sys.executable


def ensure_icon() -> str | None:
    """确保 app.ico 存在, 不存在就用 tools/make_icon.py 生成"""
    if os.path.exists(ICON):
        return ICON
    mk = os.path.join(ROOT, "tools", "make_icon.py")
    if not os.path.exists(mk):
        warn("未找到 app.ico, 也未找到 tools/make_icon.py, 跳过图标")
        return None
    info("生成应用图标 app.ico ...")
    rc = run([sys.executable, mk], quiet=True)
    if rc == 0 and os.path.exists(ICON):
        ok("图标已生成")
        return ICON
    warn("图标生成失败, 继续构建但无图标")
    return None


def check_python() -> bool:
    v = sys.version_info[:2]
    ok(f"Python {v[0]}.{v[1]}  ({sys.executable})")
    if not ((3, 9) <= v <= (3, 12)):
        warn(f"推荐使用 Python 3.9 ~ 3.12, 当前 {v[0]}.{v[1]}, 继续尝试...")
    return True


def setup_venv(use_venv: bool) -> str:
    """返回最终用于构建的解释器路径"""
    if not use_venv:
        info("跳过虚拟环境, 使用当前 Python")
        return sys.executable

    if os.path.exists(os.path.join(VENV, "Scripts", "python.exe")):
        ok("复用已有虚拟环境 .buildenv")
        return venv_python()

    info("创建构建专用虚拟环境 .buildenv ...")
    rc = run([sys.executable, "-m", "venv", VENV], quiet=True)
    if rc != 0 or not os.path.exists(os.path.join(VENV, "Scripts", "python.exe")):
        err("创建虚拟环境失败")
        print()
        print("       常见原因: Python 未安装 venv 模块, 或权限不足")
        print("       解决办法: 改用  build.bat --no-venv")
        return ""
    ok("虚拟环境已创建")
    return venv_python()


def install_deps(py: str) -> bool:
    hr("安装依赖")
    info("升级 pip ...")
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"], quiet=True)

    req_run = os.path.join(HERE, "requirements.txt")
    req_build = os.path.join(HERE, "requirements-build.txt")

    t0 = time.time()
    info("安装运行依赖 requirements.txt (最慢的一步, 请耐心等待) ...")
    if os.path.exists(req_run):
        rc = run([py, "-m", "pip", "install", "-r", req_run])
        if rc != 0:
            err("运行依赖安装失败")
            print()
            print("       国内网络可换清华源重试:")
            print(f"       {py} -m pip install -r requirements.txt \\")
            print("           -i https://pypi.tuna.tsinghua.edu.cn/simple")
            return False
    ok(f"运行依赖完成 ({time.time() - t0:.0f}s)")

    info("安装构建依赖 requirements-build.txt ...")
    if os.path.exists(req_build):
        rc = run([py, "-m", "pip", "install", "-r", req_build])
        if rc != 0:
            err("构建依赖安装失败")
            return False
    ok("构建依赖完成")
    return True


# 绝不能排除的模块：这些是第三方库（requests / urllib3 / packaging 等）
# 间接依赖的标准库。排掉它们打出来的 exe 能正常生成，
# 但一运行就 ModuleNotFoundError —— 而这类错误只有用户运行才暴露。
# 所以这里做硬性拦截，宁可多打几 MB，也不要出一个跑不起来的 exe。
NEVER_EXCLUDE = {"email", "http", "urllib", "urllib3", "ssl", "socket",
                 "json", "base64", "logging", "io", "os", "sys", "re",
                 "collections", "typing", "warnings", "tempfile", "gzip",
                 "zlib", "hashlib", "hmac", "secrets", "threading",
                 "queue", "time", "encodings", "codecs", "unicodedata",
                 "importlib", "pkgutil", "platform", "subprocess", "shutil",
                 "glob", "fnmatch", "string", "struct", "copy", "enum",
                 "dataclasses", "contextlib", "traceback", "uuid", "random"}


def validate_excludes(cmd: list) -> tuple:
    """检查排除项有没有踩到被依赖的模块，返回 (ok, 危险项列表)。"""
    bad = []
    for i, a in enumerate(cmd):
        if a == "--exclude-module" and i + 1 < len(cmd):
            m = cmd[i + 1].split(".")[0]
            if m in NEVER_EXCLUDE:
                bad.append(cmd[i + 1])
    return (not bad), bad


def _running_processes(exe_name: str) -> list:
    """列出正在运行的同名进程（Windows 用 tasklist，其他平台用 ps）。"""
    names = []
    try:
        import subprocess
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                capture_output=True, text=True, timeout=15).stdout or ""
            for line in out.splitlines():
                if line.lower().startswith(exe_name.lower()):
                    names.append(line.split()[0])
        else:
            out = subprocess.run(["ps", "-eo", "comm="], capture_output=True,
                                 text=True, timeout=15).stdout or ""
            names = [l.strip() for l in out.splitlines()
                     if l.strip().lower().startswith(exe_name.lower())]
    except Exception:
        pass
    return sorted(set(names))


def _parse_missing(line: str) -> list:
    """从自检输出里解析出 [(模块, 原因)]。

    输出形如： MISSING [('ui.dialogs', "ModuleNotFoundError('No module...')")]
    解析失败时安全返回空列表，不影响构建。
    """
    try:
        import ast
        payload = line.split("MISSING", 1)[1].strip()
        data = ast.literal_eval(payload)
        return [(str(a), str(b)) for a, b in data]
    except Exception:
        return []


def preflight_imports(py: str) -> bool:
    """打包前把客户端模块全部 import 一遍。

    目的：模块缺失/语法错误在这一步就暴露，不用等用户运行 exe 才发现。
    """
    info("导入自检 ...")
    mods = []
    for sub in ("core", "ui"):
        d = os.path.join(HERE, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py") and not fn.startswith("_"):
                mods.append(f"{sub}.{fn[:-3]}")
    if not mods:
        return True
    script = (
        "import sys; sys.path.insert(0, r'{here}');\n"
        "bad = []\n"
        "for m in {mods!r}:\n"
        "    try:\n"
        "        __import__(m)\n"
        "    except Exception as e:\n"
        "        bad.append((m, repr(e)))\n"
        "print('MISSING ' + repr(bad) if bad else 'OK')\n"
    ).format(here=HERE, mods=mods)
    fdp = os.path.join(HERE, "_preflight.py")
    out = ""
    try:
        with open(fdp, "w", encoding="utf-8") as f:
            f.write(script)
        # 注意：run() 是"实时输出并返回退出码"，拿不到 stdout，
        # 所以这里直接用 subprocess 捕获输出（build.py 里 run() 的正确用法见其他调用点）
        p = subprocess.run([py, fdp], cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        rc, out = p.returncode, (p.stdout or "")
    except Exception as e:  # noqa: BLE001
        rc, out = 1, f"{e}"
    finally:
        if os.path.exists(fdp):
            try:
                os.remove(fdp)
            except OSError:
                pass
    if rc == 0 and out.strip() == "OK":
        ok(f"导入自检通过（{len(mods)} 个模块）")
        return True

    # 区分"真缺模块"和"GUI 依赖缺失"：
    # 无显示器/精简 Python 环境下 tkinter 可能没有，但那不代表源码有问题，
    # 不能因为环境差异就阻断构建，只提示。其余缺失（含自家模块）一律中止。
    gui = ("tkinter", "customtkinter", "PIL", "_tkinter")
    fatal, soft = [], []
    for line in (out or "").splitlines():
        if not line.startswith("MISSING"):
            continue
        for mod, why in _parse_missing(line):
            if any(g in why for g in gui):
                soft.append((mod, why))
            else:
                fatal.append((mod, why))

    if soft and not fatal:
        warn("GUI 依赖缺失（不影响打包，但 exe 运行需要）：")
        for m, w in soft[:6]:
            print(f"       {m}: {w}")
        return True

    err("导入自检失败：")
    for m, w in (fatal or soft)[:10]:
        print(f"       {m}: {w}")
    print()
    print("       请先运行 python main.py 确认客户端本身能启动")
    return False


def prepare_dist() -> bool:
    """清掉上一次的 exe。

    PyInstaller 在写产物前会 os.remove 旧文件，而 Windows 上
    正在运行的 exe 无法删除，于是报
        PermissionError: [WinError 5] 拒绝访问
    绝大多数情况是用户还开着 MCSync，或者资源管理器/杀软占着 dist 目录。

    处理顺序：
      1. 发现同名进程 -> 明确报出，并尝试结束
      2. 删不掉就改名（Windows 允许给运行中的 exe 改名，只是不能删），
         让 PyInstaller 能写出新文件，不必强求用户先关程序
    """
    dist = os.path.join(HERE, "dist")
    exe = os.path.join(dist, f"{NAME}.exe")

    procs = _running_processes(f"{NAME}.exe")
    if procs:
        warn(f"检测到 MCSync.exe 正在运行（{len(procs)} 个实例）")
        print("      正在运行的 exe 无法被覆盖，需要先退出。")
        if os.name == "nt":
            try:
                import subprocess
                subprocess.run(["taskkill", "/IM", f"{NAME}.exe", "/F"],
                               capture_output=True, timeout=15)
                time.sleep(1.0)
                if not _running_processes(f"{NAME}.exe"):
                    ok("已结束正在运行的 MCSync.exe")
                else:
                    err("无法自动结束，请手动关闭 MCSync 后重新构建")
                    return False
            except Exception:
                err("无法自动结束，请手动关闭 MCSync 后重新构建")
                return False

    if not os.path.exists(exe):
        return True
    try:
        os.remove(exe)
        ok("已删除旧产物")
        return True
    except PermissionError:
        pass
    except OSError as e:
        warn(f"删除旧产物失败：{e}")

    # 删不掉 -> 改名。Windows 上改名通常可行，旧文件留着不影响新产物写入。
    for i in range(1, 100):
        stale = exe + f".old{i}"
        if os.path.exists(stale):
            continue
        try:
            os.rename(exe, stale)
            warn(f"旧产物无法删除（被占用），已改名为 {os.path.basename(stale)}")
            print("      这是上一次的构建结果，确认不需要后可自行删除。")
            return True
        except OSError:
            continue
    err("旧产物被占用且无法改名，请关闭占用 dist 目录的程序后重试")
    return False


def build(py: str, onefile: bool, windowed: bool, icon: str | None) -> bool:
    hr("构建 exe")
    if not prepare_dist():
        err("构建中止：无法清理上一次的产物")
        print()
        print("       请手动关闭 MCSync 与占用 dist 目录的程序后重试")
        return False
    info("PyInstaller 首次运行会生成缓存, 可能等待 1~3 分钟")
    print()

    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed" if windowed else "--console",
        "--onefile" if onefile else "--onedir",
        "--name", NAME,
        # 关键: 全部用绝对路径, 避免 PyInstaller 按 spec 所在目录去拼相对路径
        # 而报 "Unable to find ... build\app.ico"
    ]
    if icon:
        cmd += ["--icon", icon]
        cmd += ["--add-data", f"{icon};."]

    cmd += [
        "--collect-submodules", "customtkinter",
        # 只带真正用到的 PIL 子模块：collect 整包会塞进一堆用不到的编解码器
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "PIL._tkinter_finder",
        "--collect-submodules", "requests",
        # main.py 是入口, core / ui 是包; PyInstaller 有时分析不到, 显式声明
        "--hidden-import", "core.scanner",
        "--hidden-import", "core.sync",
        "--hidden-import", "core.api",
        "--hidden-import", "core.config",
        "--hidden-import", "core.nbt",
        "--hidden-import", "core.hasher",
        "--hidden-import", "core.models",
        "--hidden-import", "core.protocol",
        "--hidden-import", "core.transfer",
        # 这几个在函数内部延迟导入（页面切实例 / 云端补修时），
        # 显式声明，避免 PyInstaller 静态分析漏掉。
        "--hidden-import", "core.sync",
        "--hidden-import", "core.scanner",
        "--hidden-import", "core.chunk_prune",
        "--hidden-import", "core.nbt_typed",
        "--hidden-import", "core.playerdata",
        "--hidden-import", "core.secret_store",
        "--hidden-import", "core.i18n",
        "--hidden-import", "ui.main_window",
        "--hidden-import", "ui.dialogs",
        "--hidden-import", "ui.widgets",
        "--hidden-import", "ui.theme",
        "--hidden-import", "ui.design",
        "--hidden-import", "ui.prune_page",
        "--hidden-import", "ui.playerdata_page",
        "--hidden-import", "ui.login_window",
        "--hidden-import", "xxhash",
        "--hidden-import", "darkdetect",
        "--exclude-module", "tkinter.tix",
        "--exclude-module", "tkinter.test",
        # ---- 排除项：只排除"实测无人使用"的模块，见 tools/test_build_excludes.py
        #
        # 曾经凭直觉排除了 email / html / sqlite3 等标准库，结果 urllib3 依赖
        # email（HTTP 头解析走 email.parser / email.utils），打包出的 exe 一运行
        # 就 ModuleNotFoundError。这类"以为没人用、其实被间接依赖"的坑很难靠
        # 肉眼发现，所以现在改为：白名单写死 + 构建前自动校验。
        "--exclude-module", "pydoc",
        "--exclude-module", "pydoc_data",
        "--exclude-module", "doctest",
        "--exclude-module", "lib2to3",
        "--exclude-module", "distutils",
        "--exclude-module", "setuptools",
        "--exclude-module", "curses",
        "--exclude-module", "IPython",
        "--exclude-module", "notebook",
        "--exclude-module", "tkinter.tix",
        "--exclude-module", "tkinter.test",
        "--exclude-module", "test",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--workpath", os.path.join(HERE, "build"),
        "--distpath", os.path.join(HERE, "dist"),
        "--specpath", HERE,          # spec 放当前目录, 避免相对路径基准漂移
        ENTRY,
    ]

    # UPX 能再压 30%~50%，装了就自动启用；没装也不影响产物可用性
    upx = shutil.which("upx")
    if upx:
        cmd.insert(1, "--upx-dir")
        cmd.insert(2, os.path.dirname(upx))
        info(f"检测到 UPX，启用压缩：{upx}")
    else:
        info("未检测到 UPX（可选，装上可再省 30%~50% 体积）")

    ok_flag, bad = validate_excludes(cmd)
    if not ok_flag:
        err("排除项踩到了被依赖的模块，已中止：" + ", ".join(bad))
        print()
        print("       这些模块会被第三方库（requests / urllib3 等）间接引用，")
        print("       排掉后 exe 能生成但一运行就 ModuleNotFoundError。")
        return False

    if not preflight_imports(py):
        return False

    t0 = time.time()
    rc = run(cmd)
    if rc != 0:
        err("构建失败, 请查看上方报错")
        print()
        print("       排错建议:")
        print("       1. 加 --console 重跑, 保留控制台看完整报错")
        print("       2. 关闭杀毒软件, Windows Defender 常误报")
        print("       3. 关闭占用 dist 目录的程序, 例如资源管理器预览")
        print("          （若提示 WinError 5, 通常是 MCSync.exe 还在运行）")
        print("       4. 尝试  build.bat --dir --no-venv")
        print("       5. 删除 build 与 dist 目录后重试")
        return False

    out = os.path.join(HERE, "dist", f"{NAME}.exe" if onefile else NAME, *([] if onefile else [f"{NAME}.exe"]))
    hr("构建成功")
    if onefile:
        print(f"       输出: dist\\{NAME}.exe")
    else:
        print(f"       输出: dist\\{NAME}\\{NAME}.exe")
    if os.path.exists(out):
        print(f"       大小: {os.path.getsize(out) / 1048576:.1f} MB")
    print(f"       耗时: {time.time() - t0:.0f}s")
    print()
    print("       首次启动稍慢, 因为要解压临时文件, 之后就快了")
    print()
    print("       提示: 若双击 exe 没反应, 先运行一次 main.py")
    print("             确认客户端本身能启动, 以区分打包问题与代码问题")
    print()
    return True


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--dir", action="store_true", help="文件夹模式")
    ap.add_argument("--no-venv", action="store_true", help="不使用虚拟环境")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口")
    ap.add_argument("--help", "-h", action="store_true")
    args, _ = ap.parse_known_args()

    if args.help:
        print()
        print("  MCSync 客户端构建脚本")
        print()
        print("    build.bat               单文件 exe, 默认")
        print("    build.bat --dir         文件夹模式, 启动更快")
        print("    build.bat --no-venv    不使用虚拟环境")
        print("    build.bat --console    保留控制台, 用于排错")
        print("    build.bat --help       显示本帮助")
        print()
        return 0

    print()
    print("  " + "=" * 56)
    print("    MCSync 客户端一键构建")
    print("  " + "=" * 56)
    print()

    check_python()
    py = setup_venv(not args.no_venv)
    if not py:
        return 1
    if py != sys.executable:
        ok(f"已切换到虚拟环境: {py}")

    icon = ensure_icon()

    if not install_deps(py):
        return 1

    return 0 if build(py, not args.dir, not args.console, icon) else 1


if __name__ == "__main__":
    sys.exit(main())
