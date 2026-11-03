# -*- coding: utf-8 -*-
"""
MCSync 一键推送到 GitHub

设计要点：
  1) 提交前做安全扫描，任何命中敏感规则的文件都会被拦下并提示，
     绝不会把口令、私钥、用户存档、打包产物推上去。
  2) 检测并处理 GitHub 认证方式（HTTPS 需要 PAT，SSH 需要密钥）。
  3) 分支自动识别（main / master），首次推送自动建立上游关联。
  4) 全程中文输出，由 Python 负责编码，规避 cmd 的多字节截断问题。

用法：
  python push.py                      交互式确认后提交并推送
  python push.py -m "修复某问题"       指定提交信息
  python push.py --dry-run            只做检查，不提交不推送
  python push.py --yes                跳过确认直接推送
  python push.py --remote <url>       指定/修改远端地址
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_URL = "https://github.com/radix-Lunaris/MCsaves-sync.git"

C_GRN = "\033[32m"
C_RED = "\033[31m"
C_YEL = "\033[33m"
C_DIM = "\033[2m"
C_BLD = "\033[1m"
C_RST = "\033[0m"


def ok(msg):
    print(f"{C_GRN}[OK]{C_RST} {msg}")


def info(msg):
    print(f"  {C_DIM}*{C_RST} {msg}")


def warn(msg):
    print(f"{C_YEL}[!]{C_RST} {msg}")


def err(msg):
    print(f"{C_RED}[X]{C_RST} {msg}")


def step(msg):
    print(f"\n{C_BLD}==>{C_RST} {msg}")


# 推 GitHub 需要代理（本机直连 GitHub 常常超时）；客户端同步不需要。
DEFAULT_PROXY = "http://127.0.0.1:10808"
PROXY = os.environ.get("MCSYNC_PUSH_PROXY", DEFAULT_PROXY)


def _proxy_env():
    """给 git 子进程注入代理。

    只作用于本次子进程，不写 git config --global，
    因此不会影响你其他仓库的推送行为。
    """
    if getattr(_proxy_env, "disabled", False):
        return None
    url = (PROXY or "").strip()
    if not url:
        return None
    env = os.environ.copy()
    env["http_proxy"] = url
    env["https_proxy"] = url
    env["HTTP_PROXY"] = url
    env["HTTPS_PROXY"] = url
    return env


def run(args, capture=True, check=False, cwd=ROOT):
    """执行命令，返回 (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(
            args, cwd=str(cwd), capture_output=capture, text=True,
            encoding="utf-8", errors="replace", env=(_proxy_env() or None),
        )
        if check and p.returncode != 0:
            raise RuntimeError(f"命令失败: {' '.join(args)}\n{p.stderr}")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"找不到命令: {args[0]}"


def check_proxy():
    """检测代理是否可用（只做网络探测，不动 git）"""
    url = (PROXY or "").strip()
    print(f"\n==> 代理检测")
    if not url:
        print(f"  {C_YEL}未配置代理（--no-proxy）{C_RST}")
        print("  若推送 GitHub 超时，去掉 --no-proxy 或指定 --proxy")
        return False
    print(f"  代理地址: {url}")
    try:
        import urllib.request
        req = urllib.request.Request("https://api.github.com", method="HEAD")
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url}),
            urllib.request.HTTPSHandler(context=ssl_ctx()),
        )
        with op.open(req, timeout=15) as r:
            print(f"  {C_GRN}[OK]{C_RST} 经代理可访问 GitHub（HTTP {r.status}）")
            return True
    except Exception as e:
        print(f"  {C_RED}[X]{C_RST} 无法经代理访问 GitHub: {e}")
        print(f"\n  请确认代理软件已启动，端口是不是 {url.split(':')[-1]}")
        print(f"  Clash 常见 7890 / 10808，v2ray 常见 10808")
        print(f"  换端口： push.bat --proxy http://127.0.0.1:7890")
        print(f"  不走代理： push.bat --no-proxy")
        return False


def ssl_ctx():
    import ssl
    return ssl.create_default_context()


# ---------------------------------------------------------------- 安全扫描
# 这些规则是"绝不能进仓库"的硬红线。.gitignore 负责日常拦，
# 这里做第二道闸 —— 万一文件在 .gitignore 生效前就已被 git 跟踪，
# 光靠 .gitignore 是拦不住的（已跟踪文件会继续被提交）。
BLOCK_PATTERNS = [
    (r"(^|/)mcsync\.env$", "服务端口令与环境变量"),
    (r"(^|/)\.env$", "环境变量文件（可能含密钥）"),
    (r"(^|/)token\.txt$", "服务端口令明文"),
    (r"(^|/)clients\.json$", "已配对客户端私钥"),
    (r"(^|/)accounts\.json$", "账号库（含口令哈希）"),
    (r"(^|/)config\.json$", "客户端本地配置（含服务器地址与私钥）"),
    (r"\.pem$|\.key$|\.p12$|\.pfx$", "证书或私钥文件"),
    (r"(^|/)id_rsa|(^|/)id_ed25519", "SSH 私钥"),
    (r"\.exe$|\.msi$|\.pyd$|\.so$", "编译产物"),
    (r"(^|/)(data|serverdata|blobs|manifests)(/|$)", "服务端运行时数据（含用户存档）"),
    (r"\.db$|\.sqlite3?$", "数据库文件"),
    (r"(^|/)(dist|build|\.buildenv|venv|\.venv)(/|$)", "构建产物或虚拟环境"),
    (r"(^|/)_MCSyncBackups(/|$)", "本地存档备份（体积大且属隐私）"),
]

# 超过这个体积的文件提醒一次（GitHub 单文件上限 100MB，这里设得保守些）
BIG_FILE_BYTES = 20 * 1024 * 1024


def scan_blocked(files):
    """返回 [(文件, 命中原因)]"""
    hits = []
    for f in files:
        norm = f.replace("\\", "/")
        for pat, why in BLOCK_PATTERNS:
            if re.search(pat, norm):
                hits.append((f, why))
                break
    return hits


def scan_big(files):
    out = []
    for f in files:
        p = ROOT / f
        try:
            if p.is_file() and p.stat().st_size > BIG_FILE_BYTES:
                out.append((f, p.stat().st_size))
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- Git 准备
def ensure_git():
    rc, _, _ = run(["git", "--version"])
    if rc != 0:
        err("未检测到 Git。请先安装：https://git-scm.com/downloads")
        sys.exit(1)
    rc, out, _ = run(["git", "--version"])
    ok(f"Git {out.replace('git version', '').strip()}")


def ensure_repo():
    rc, out, _ = run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        step("初始化本地仓库")
        run(["git", "init"], check=True)
        ok("已执行 git init")
    else:
        info("已是 Git 仓库")


def ensure_branch():
    """确定推送分支。GitHub 默认分支现在是 main，
    新仓库统一用它，避免推上去变成多一个 master 分支。"""
    # 是否已有提交
    rc_commit, _, _ = run(["git", "rev-parse", "--verify", "HEAD"])
    has_commit = (rc_commit == 0)

    rc, out, _ = run(["git", "symbolic-ref", "--short", "HEAD"])
    br = out.strip() if rc == 0 else ""

    if not br:
        br = "main"
        run(["git", "checkout", "-b", br])
        ok(f"已创建分支 {br}")
        return br

    # 还没提交过时，顺手把默认分支名统一成 main
    if not has_commit and br != "main":
        run(["git", "branch", "-M", "main"])
        ok(f"已将默认分支重命名为 main（原 {br}）")
        br = "main"

    info(f"当前分支：{br}")
    return br


def ensure_remote(url):
    rc, out, _ = run(["git", "remote", "get-url", "origin"])
    cur = out.strip()
    if rc != 0 or not cur:
        run(["git", "remote", "add", "origin", url], check=True)
        ok(f"已添加远端 origin -> {url}")
    elif cur.rstrip("/").lower() != url.rstrip("/").lower():
        warn(f"远端 origin 当前指向 {cur}")
        run(["git", "remote", "set-url", "origin", url], check=True)
        ok(f"已更新远端 origin -> {url}")
    else:
        info(f"远端 origin：{cur}")


def ensure_identity():
    rc, name, _ = run(["git", "config", "user.name"])
    rc2, mail, _ = run(["git", "config", "user.email"])
    if rc != 0 or not name:
        run(["git", "config", "user.name", "radix-Lunaris"], check=True)
        ok("已设置 user.name = radix-Lunaris")
    else:
        info(f"user.name = {name}")
    if rc2 != 0 or not mail:
        mail = "radix-Lunaris@users.noreply.github.com"
        run(["git", "config", "user.email", mail], check=True)
        ok(f"已设置 user.email = {mail}")
    else:
        info(f"user.email = {mail}")


def check_gitignore():
    gi = ROOT / ".gitignore"
    if not gi.exists():
        err("缺少 .gitignore，已中止（没有它很容易误传敏感文件）")
        sys.exit(1)
    ok(".gitignore 已就位")


def check_license():
    lic = ROOT / "LICENSE"
    if not lic.exists():
        warn("缺少 LICENSE 文件")
        return False
    txt = lic.read_text(encoding="utf-8", errors="replace")
    if "GNU GENERAL PUBLIC LICENSE" in txt and "Version 3" in txt:
        ok("LICENSE 为 GPLv3 全文")
        return True
    warn("LICENSE 内容看起来不像 GPLv3，请确认")
    return False


# ---------------------------------------------------------------- 提交
def has_unpushed(branch: str) -> bool:
    """本地是否有尚未推送到远端的提交。

    新仓库（远端还没有这个分支）时，@{upstream} 不存在，
    此时只要本地有提交就算"待推送"。
    """
    rc, out, _ = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and out.strip() and out.strip() != "@{u}":
        rc2, out2, _ = run(["git", "log", "--oneline", "@{u}..HEAD"])
    else:
        rc2, out2, _ = run(["git", "log", "--oneline", "HEAD", "--not", "--remotes"])
    if rc2 != 0:
        # 远端还没有任何相关引用，只要有提交就待推送
        rc3, out3, _ = run(["git", "log", "--oneline", "-1"])
        return rc3 == 0 and bool(out3.strip())
    return bool(out2.strip())


def staged_files():
    rc, out, _ = run(["git", "diff", "--cached", "--name-only"])
    return [l for l in out.splitlines() if l.strip()]


def working_files():
    """将被 git add . 纳入的文件（含未跟踪与已修改）"""
    rc, out, _ = run(["git", "status", "--porcelain"])
    names = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # 处理重命名 "R  old -> new"
        if " -> " in line:
            line = line.split(" -> ", 1)[1]
        names.append(line[3:].strip().strip('"'))
    return [n for n in names if n]


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("-m", "--message", default="", help="提交信息")
    ap.add_argument("--remote", default=REPO_URL, help="远端仓库地址")
    ap.add_argument("--branch", default="", help="推送分支（默认自动识别）")
    ap.add_argument("--proxy", default=None,
                    help=f"推送用的代理地址，默认 {DEFAULT_PROXY}")
    ap.add_argument("--no-proxy", action="store_true", help="推送时不走代理")
    ap.add_argument("--check-proxy", action="store_true",
                    help="只检测代理是否可用，不做任何 git 操作")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不提交不推送")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("-f", "--force", action="store_true",
                    help="首次推送就直接用本地覆盖远端")
    ap.add_argument("--no-force", action="store_true",
                    help="被拒绝时不做强制覆盖，改为提示手动处理")
    args = ap.parse_args()

    print(f"\n{C_BLD}{'=' * 62}{C_RST}")
    print(f"{C_BLD}  MCSync 一键推送  ->  {args.remote}{C_RST}")
    print(f"{C_BLD}{'=' * 62}{C_RST}")

    step("环境检查")
    if args.proxy is not None:
        global PROXY
        PROXY = args.proxy
    if args.no_proxy:
        _proxy_env.disabled = True
        PROXY = ""

    if args.check_proxy:
        check_proxy()
        return

    ensure_git()
    check_gitignore()
    check_license()

    ensure_repo()
    ensure_identity()
    branch = args.branch.strip() or ensure_branch()
    ensure_remote(args.remote)

    # ------------------------------------------------------------ 预扫描
    step("暂存文件")
    run(["git", "add", "-A"], check=True)
    files = staged_files()
    if not files:
        # 没有新改动，不代表不用推送：本地可能已有提交还没推上去。
        # 旧版本在这里直接 return，导致"提交成功了但每次都显示无需推送"。
        if has_unpushed(branch):
            info("工作区无新改动，但存在尚未推送的提交")
            skip_commit = True
        else:
            info("没有需要提交的改动")
            print(f"\n{C_DIM}工作区已是最新，无需推送。{C_RST}")
            return
    else:
        skip_commit = False

    step(f"安全扫描（{len(files)} 个待提交文件）")
    blocked = scan_blocked(files)
    if blocked:
        err("以下文件命中敏感规则，已中止推送：")
        for f, why in blocked:
            print(f"      {C_RED}{f}{C_RST}  ->  {why}")
        print(f"\n{C_YEL}处理建议：{C_RST}")
        print("   1. 确认这些文件确实不需要进仓库")
        print("   2. 若已被 git 跟踪，需要先移出跟踪：")
        for f, _ in blocked[:5]:
            print(f"      git rm --cached \"{f}\"")
        print("   3. 确认 .gitignore 已包含对应规则\n")
        # 回滚暂存，避免留下脏状态
        run(["git", "reset"])
        sys.exit(1)
    ok("未发现敏感文件")

    big = scan_big(files)
    if big:
        warn("以下文件体积较大，建议确认是否有必要提交：")
        for f, size in big:
            print(f"      {f}  {size / 1024 / 1024:.1f} MB")
    else:
        info("无超大文件")

    # ------------------------------------------------------------ 展示
    step("待提交内容")
    shown = files[:40]
    for f in shown:
        print(f"      {f}")
    if len(files) > len(shown):
        print(f"      {C_DIM}... 另有 {len(files) - len(shown)} 个文件{C_RST}")

    if args.dry_run:
        run(["git", "reset"])
        print(f"\n{C_YEL}[试运行]{C_RST} 已完成检查，未提交也未推送。")
        return

    if not args.yes:
        print()
        try:
            ans = input("   确认提交并推送？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            run(["git", "reset"])
            warn("已取消（暂存已回滚，工作区未改动）")
            return

    # ------------------------------------------------------------ 提交
    if skip_commit:
        info("跳过提交（无新改动）")
    else:
        step("提交")
    msg = args.message.strip()
    if not msg:
        import time
        msg = f"更新 MCSync 源码 {time.strftime('%Y-%m-%d %H:%M')}"
    if not skip_commit:
        rc, out, errout = run(["git", "commit", "-m", msg])
        if rc != 0:
            if "nothing to commit" in (out + errout).lower():
                info("没有可提交的改动")
            else:
                err(f"提交失败：{errout or out}")
                sys.exit(1)
        else:
            ok(f"已提交：{msg}")

    # ------------------------------------------------------------ 推送
    step(f"推送到 origin/{branch}")
    if _proxy_env():
        info(f"经代理推送：{PROXY}")
        # 先探一次，代理没开就别让用户对着 git 的原始报错猜
        if not check_proxy():
            warn("代理不可用，仍会尝试直连；若失败可改用 --no-proxy 或 --proxy <地址>")
    else:
        info("直连推送（未使用代理）")
    if args.force and not args.no_force:
        info("已指定 --force，直接覆盖远端")
        rc, out, errout = run(["git", "push", "-u", "--force-with-lease", "origin", branch])
        if rc != 0 and ("stale info" in (out + errout).lower()
                        or "cannot lock ref" in (out + errout).lower()):
            rc, out, errout = run(["git", "push", "-u", "--force", "origin", branch])
    else:
        rc, out, errout = run(["git", "push", "-u", "origin", branch])
    combined = (out + errout)

    if rc == 0:
        ok("推送成功")
        print(f"\n{C_GRN}仓库地址：{C_RST}https://github.com/radix-Lunaris/MCsaves-sync")
        return

    # 远端有本地没有的提交（新仓库常见的"只有 README/LICENSE"）。
    # 默认直接用本地覆盖：本仓库是单人维护，远端那份初始占位没有保留价值。
    # --no-force 可关掉这个行为，改为交给人处理。
    is_rejected = ("rejected" in combined.lower()
                   or "non-fast-forward" in combined.lower()
                   or "fetch first" in combined.lower())

    if is_rejected and not args.no_force:
        warn(f"远端有本地没有的提交，按默认策略用本地覆盖（{branch}）")
        rc, out, errout = run(["git", "push", "-u", "--force-with-lease", "origin", branch])
        combined = (out + errout)
        if rc == 0:
            ok("已强制覆盖并推送成功")
            print(f"\n{C_GRN}仓库地址：{C_RST}https://github.com/radix-Lunaris/MCsaves-sync")
            print(f"{C_DIM}注：远端原有的提交（如初始 README）已被本地历史替换。{C_RST}")
            return
        # 首次推送时本地还没有远程跟踪引用，--force-with-lease 会拒绝，
        # 这种情况退化为 --force 是安全的（没有可丢失的跟踪信息）。
        if "stale info" in combined.lower() or "cannot lock ref" in combined.lower():
            info("本地尚无远程跟踪引用，改用 --force 重试")
            rc, out, errout = run(["git", "push", "-u", "--force", "origin", branch])
            combined = (out + errout)
            if rc == 0:
                ok("已强制覆盖并推送成功")
                print(f"\n{C_GRN}仓库地址：{C_RST}https://github.com/radix-Lunaris/MCsaves-sync")
                print(f"{C_DIM}注：远端原有的提交（如初始 README）已被本地历史替换。{C_RST}")
                return

    # 失败：给出针对性的解决办法
    err("推送失败")
    low = combined.lower()
    if "could not read username" in low or "authentication failed" in low \
            or "invalid username or password" in low or "403" in low:
        print(f"\n{C_YEL}这是认证问题。GitHub 已不支持账号密码，请用以下任一方式：{C_RST}")
        print("   A) 个人访问令牌（推荐，最简单）")
        print("      1) GitHub → Settings → Developer settings")
        print("         → Personal access tokens → Tokens (classic)")
        print("      2) Generate new token，勾选 repo 权限")
        print("      3) 推送时用户名填 GitHub 账号，密码粘贴这个令牌")
        print()
        print("   B) 改用 SSH（配好后免密）")
        print("      git remote set-url origin git@github.com:radix-Lunaris/MCsaves-sync.git")
        print("      并把本机公钥加到 GitHub → Settings → SSH and GPG keys")
        print()
        print("   C) 用 GitHub CLI 托管凭据")
        print("      winget install GitHub.cli  然后  gh auth login")
    elif is_rejected:
        if args.no_force:
            print(f"\n{C_YEL}远端有本地没有的提交，且已用 --no-force 关闭自动覆盖。{C_RST}")
        else:
            print(f"\n{C_YEL}远端有本地没有的提交（已尝试强制覆盖仍失败）。{C_RST}")
        print(f"   手动处理：git pull --rebase origin {branch}  然后重新运行 push.bat")
        print(f"   或确认无误后：git push --force origin {branch}")
    elif "network" in low or "timed out" in low or "could not resolve" in low:
        print(f"\n{C_YEL}网络不通。请检查代理或网络后重试。{C_RST}")
        print("      git config --global http.proxy http://127.0.0.1:7890   (按你的代理端口改)")
    else:
        print(f"\n{C_DIM}{combined[:800]}{C_RST}")
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
