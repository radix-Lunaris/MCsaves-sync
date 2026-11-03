# -*- coding: utf-8 -*-
"""
界面多语言。

设计取舍：
  * 源码里的字符串本身就是中文，所以中文模式下 t() 直接原样返回，
    不需要维护一份中文词条 —— 少一处要同步的地方。
  * 英文模式查表，查不到就退回中文（而不是显示空白或 key），
    这样新增界面文案时不会立刻变成一堆看不懂的占位符。
  * 词条直接内嵌在模块里，不作为外部 json 资源，
    省掉 PyInstaller 的数据文件打包配置。
"""
from __future__ import annotations

from typing import Dict

LANGUAGES: Dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
}

_current = "zh"

# 中文 -> 英文
# 键必须与源码里的字面量完全一致；漏翻的会安全退回中文。
_EN: Dict[str, str] = {
    "只扫上次命中的目录，不阻塞界面；嫌慢可关闭":
        "Only re-scans known folders without blocking; turn off if unwanted",
    "打开软件时在后台刷新一次实例列表":
        "Refresh instance list in background on launch",
    "启动时自动刷新": "Refresh on start",
    "  ⚙  设置": "  ⚙  Settings",
    "0 行": "0 lines",
    "MCSync 已启动": "MCSync started",
    "Minecraft 存档云同步工具 v1.0": "Minecraft Save Sync Tool v1.0",
    "● 未连接": "● Not connected",
    "● 连接中…": "● Connecting…",
    "● 连接失败": "● Connection failed",
    "⚙  同步整合包配置": "⚙  Sync Modpack Config",
    "⚠ 检测到文件冲突": "⚠ File conflicts detected",
    "➕ 选择文件夹": "➕ Select folder",
    "⟳ 刷新": "⟳ Refresh",
    "⟳ 实例重扫": "⟳ Rescan Instances",
    "⬆  上传到云端": "⬆  Upload to Cloud",
    "⬇  下载到本地": "⬇  Download to Local",
    "一行一个目录，用于手动指定 PCL2 / HMCL 等绿色版所在位置":
        "One directory per line, for manually specifying portable launchers like PCL2 / HMCL",
    "下载": "Download",
    "云端存档": "Cloud Saves",
    "云端暂无存档": "No saves in cloud yet",
    "保存": "Save",
    "保存为（存档文件夹名）：": "Save as (save folder name):",
    "保存日志": "Save Log",
    "允许在游戏运行时同步（可能读到半截数据，不建议）":
        "Allow syncing while the game is running (may read half-written data, not recommended)",
    "全部设为：": "Set all to:",
    "分块大小（MB）": "Piece size (MB)",
    "分钟一次（最少 5）": "minutes interval (min 5)",
    "删除": "Delete",
    "删除云端": "Delete on Cloud",
    "取消": "Cancel",
    "回滚": "Rollback",
    "在默认规则之外追加的规则（glob 语法，一行一条）":
        "Additional rules beyond the defaults (glob syntax, one per line)",
    "备份保留份数": "Backups to keep",
    "外观点": "Appearance",
    "存档": "Saves",
    "存档云同步": "Save Cloud Sync",
    "完整路径：": "Full path:",
    "定时同步": "Scheduled Sync",
    "定时同步：开始自动上传": "Scheduled sync: starting auto upload",
    "实例扫描深度": "Instance scan depth",
    "就绪": "Ready",
    "已删除": "Deleted",
    "已扫描的候选根目录：": "Scanned candidate roots:",
    "已添加目录，正在重新扫描…": "Directory added, rescanning…",
    "已请求取消当前任务…": "Cancellation requested…",
    "并发线程数": "Concurrency",
    "开启后按间隔自动上传当前选中的存档；关闭窗口即停止。":
        "When enabled, the selected save is uploaded automatically at the interval; "
        "stops when the window closes.",
    "开启（默认关闭）": "Enable (off by default)",
    "开始下载": "Start Download",
    "或直接输入整合包名：": "Or type the modpack name directly:",
    "打开配置目录": "Open Config Directory",
    "提示：绿色版启动器请在「设置 → 额外实例目录」手动添加":
        "Tip: for portable launchers, add their folder manually in Settings → Extra Instance Directories",
    "支持 * ? 通配，目录以 / 结尾，! 开头表示强制包含":
        "Supports * ? wildcards; directories end with /; a leading ! forces inclusion",
    "日志同时保存在内存中，最多保留 2000 行；点「保存日志」可导出为 .log 文件":
        "Logs are also kept in memory, up to 2000 lines; click Save Log to export a .log file",
    "日志已保存": "Log saved",
    "暂无存档": "No saves",
    "服务器地址": "Server Address",
    "服务器地址，如 http://192.168.1.10:8650": "Server address, e.g. http://192.168.1.10:8650",
    "未同步": "Not synced",
    "未自动识别到实例，可点 ➕ 手动添加目录":
        "No instances detected; click ➕ to add a directory manually",
    "未连接": "Not connected",
    "未选择存档": "No save selected",
    "本地缓存": "Local Cache",
    "本机没有同名的整合包实例，无法装载配置":
        "No local modpack instance with the same name; cannot load config",
    "极限": "Hardcore",
    "正在取消…": "Cancelling…",
    "正在读取存档…": "Reading save…",
    "浏览": "Browse",
    "清理块缓存": "Clean Piece Cache",
    "游戏实例": "Game Instances",
    "版本": "Version",
    "版本管理": "Version Manager",
    "界面缩放": "UI Scaling",
    "留空则使用上面的下拉选择": "Leave empty to use the dropdown above",
    "登录口令": "Password",
    "登录账号": "Username",
    "直连（客户端不走任何代理）": "Direct (client does not use any proxy)",
    "确定": "OK",
    "确定，继续同步": "OK, continue syncing",
    "示例：http://1.2.3.4:36854 ；局域网填内网 IP，公网建议加 HTTPS 反代":
        "Example: http://1.2.3.4:36854 ; use the LAN IP locally, "
        "an HTTPS reverse proxy for public access",
    "管理员分配给你的账号": "Account assigned by the administrator",
    "缓存实例已失效，转为全盘扫描…": "Cached instances are stale, falling back to a full scan…",
    "网络方式": "Network Mode",
    "若启动器是绿色版且不在上述目录，请在设置里手动添加其所在文件夹":
        "If your launcher is portable and not in the directories above, add its folder manually in Settings",
    "装到这个实例：": "Install into this instance:",
    "设置已保存": "Settings saved",
    "请先在中间选择一个本地存档": "Please select a local save first",
    "请先在左侧选择一个游戏实例": "Please select a game instance on the left first",
    "请先填写并测试服务器地址": "Please fill in and test the server address first",
    "请先填写服务器地址": "Please fill in the server address first",
    "请先选中一个云端存档": "Please select a cloud save first",
    "请先选择云端存档": "Please select a cloud save first",
    "请先选择右侧的云端存档": "Please select a cloud save on the right",
    "请先选择左侧的游戏实例": "Please select a game instance on the left",
    "请填写服务器地址": "Please fill in the server address",
    "越大越慢；找不到实例时可调到 4~5": "Higher is slower; try 4~5 if instances are not found",
    "连接测试": "Connection Test",
    "选择保存位置": "Choose Save Location",
    "选择启动器或实例所在目录": "Select the launcher or instance directory",
    "选择启动器目录或游戏目录": "Select the launcher or game directory",
    "选择它所属的整合包（实例）。留空表示归入「未分类」。":
        "Choose the modpack (instance) it belongs to. Leave empty for Uncategorized.",
    "重新登录": "Re-login",
    "额外实例目录": "Extra Instance Directories",
    "🏷 归类": "🏷 Categorize",
    "📂 打开目录": "📂 Open Directory",
    "📜 传输日志": "📜 Transfer Log",
    "📜 日志": "📜 Log",
    "🔍 全盘重扫": "🔍 Full Rescan",
    "已登录": "Logged in",
    "登录成功": "Login succeeded",
    "登录失败": "Login failed",
    "登录失败，请检查账号与口令": "Login failed — please check username and password",
    "请先填写账号与口令": "Please enter username and password first",
    "管理员": "Admin",
    "普通用户": "User",
    "服务器地址（如 http://1.2.3.4:36854）": "Server address (e.g. http://1.2.3.4:36854)",
    "账号": "Username",
    "口令": "Password",
    "登录": "Login",
    "导航": "Navigate",
    "界面语言": "Interface Language",
    "语言切换将在重启客户端后完全生效": "Language change takes full effect after restarting the client",
}


def set_lang(code: str) -> None:
    """设置当前语言；未知代码退回中文。"""
    global _current
    _current = code if code in LANGUAGES else "zh"


def get_lang() -> str:
    return _current


def t(text: str) -> str:
    """翻译一段界面文案。

    中文模式原样返回；英文模式查表，查不到就退回原文，
    避免新增文案时界面出现空白或占位符。
    """
    if _current == "zh" or not text:
        return text
    return _EN.get(text, text)
