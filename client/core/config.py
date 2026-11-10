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
MCSync 客户端配置（单例，自动持久化到 JSON）

存放位置：系统级应用数据目录（不是程序所在目录）
  Windows : %LOCALAPPDATA%\MCSync\config.json
  macOS   : ~/Library/Application Support/MCSync/config.json
  Linux   : ~/.config/MCSync/config.json
可用环境变量 MCSYNC_CONFIG_DIR 覆盖。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from typing import Any, Dict

APP_DIR_NAME = "MCSync"
CONFIG_NAME = "config.json"


def _system_app_dir() -> str:
    """系统级应用数据目录。

    为什么要改：旧版本打包成 exe 后会把 config.json 写到 exe 同目录，
    带来两个问题——换目录放程序等于配置丢失；装到 Program Files 时
    普通用户没有写权限会直接失败。现在统一走系统规定的应用数据目录。
    """
    try:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if not base:
                base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
            return os.path.join(base, APP_DIR_NAME)
        if sys.platform == "darwin":
            return os.path.join(os.path.expanduser("~"), "Library",
                                "Application Support", APP_DIR_NAME)
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".config")
        return os.path.join(base, APP_DIR_NAME)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "." + APP_DIR_NAME.lower())


def _default_dir() -> str:
    """配置目录：始终使用系统目录，不放程序所在目录。"""
    forced = os.environ.get("MCSYNC_CONFIG_DIR", "").strip()
    if forced:
        return forced
    d = _system_app_dir()
    # 系统目录不可写时（极少见）退回用户主目录，绝不落到程序目录
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".writetest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
    except Exception:
        d = os.path.join(os.path.expanduser("~"), "." + APP_DIR_NAME.lower())
    return d


DEFAULTS: Dict[str, Any] = {
    "server_url": "http://127.0.0.1:8650",
    "server_token": "",              # 已废弃：旧版服务器随机口令（现改为账号登录）
    "username": "",                  # 登录账号（管理员在面板创建）
    # 登录口令不在这里存明文：写入一律走 set_secret("password", ...) 进系统加密区。
    # 保留这个键只是为了兼容极老的配置，加载后会被迁移并清除。
    "password": "",
    "client_id": "",                 # 本机标识（配对后写入）
    "client_secret": "",             # 客户端私钥（本地生成，永不上网）
    "paired_server": "",             # 私钥对应的服务器地址
    # 代理策略：默认走本地代理 127.0.0.1:10808（Clash / v2ray 常见端口）。
    # 本机直连公网服务器常常连不上，走本地代理反而能出去。
    # 客户端一律直连：连自己的服务器不需要代理，走本地代理反而连不上。
    # 需要代理的是 push.bat 推 GitHub，那是 git 侧的事，与客户端无关。
    "proxy_mode": "off",
    "language": "zh",           # zh / en
    # ---- 登录凭据
    "username": "",
    "auto_login": True,         # 启动自动登录（需已记住口令）
    "remember_password": True,  # 记住口令
    "logged_in": False,         # 上次是否登录成功（仅用于界面初始态）
    # 定时同步：默认关闭，用户显式开启才生效
    "auto_sync_enabled": False,
    "auto_sync_minutes": 30,
    # 游戏运行中是否仍允许同步（默认否，避免读到半截数据坏档）
    "allow_sync_while_running": False,
    "theme_mode": "dark",            # dark / light / system
    "ui_scale": 1.0,                 # 自动计算后写入
    "auto_scale": True,
    "last_instance": "",
    "known_instances": [],          # 上次扫描到的实例（含 .minecraft 路径），用于快速恢复
    "known_roots": [],              # 上次扫描到的实例根所在目录，供「实例重扫」使用
    "scanned_once": False,
    "rescan_on_start": True,        # 启动时后台重扫一次已知目录（关闭则完全跳过）
    "last_save": "",
    "last_cloud_save": "",
    "custom_roots": [],              # 用户手动添加的实例根目录
    "ignored_roots": [],
    "scan_depth": 3,
    "ignore_rules": [],              # 额外忽略规则（追加到默认规则之上）
    "core_only": False,              # 只同步存档核心内容
    "force_full_hash": False,        # 忽略 size+mtime 预筛，每次重算哈希（更慢但更准）
    "chunk_size_mb": 4,
    "concurrency": 4,
    "backup_before_sync": True,
    "keep_backups": 5,
    "backup_dir": "",                # 空 = <存档同级>/_MCSyncBackups
    "delete_remote_on_upload": True, # 本地删了 -> 云端也删
    "delete_local_on_download": True,# 云端删了 -> 本地也删
    # 共享玩家数据：上传前自动摘除 level.dat 里内嵌的房主玩家数据，
    # 让云端版本天然「共享友好」；下载时再兜底修一次历史版本。
    "auto_fix_playerdata": True,
    # 可选：自己的 UUID。填了之后，若内嵌数据本就属于你，则原样保留不动；
    # 留空时一律走「归档 + 摘除」，对房主同样安全（数据会被归档回来）。
    "my_uuid": "",
    "keep_versions_hint": 0,
    "window_geometry": "",
}


class Config:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls) -> "Config":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self.dir = _default_dir()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            self.dir = os.path.join(os.path.expanduser("~"), "." + APP_DIR_NAME.lower())
            os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, CONFIG_NAME)
        self._migrate_legacy()
        self.data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    def _migrate_legacy(self) -> None:
        """把旧版本留在程序目录的配置搬到系统目录，避免升级后配置丢失。"""
        if os.path.exists(self.path):
            return
        cands = []
        try:
            if getattr(sys, "frozen", False):
                cands.append(os.path.join(os.path.dirname(sys.executable), CONFIG_NAME))
            # 源码运行时的旧位置（core/ 上级目录）
            cands.append(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CONFIG_NAME))
        except Exception:
            return
        for src in cands:
            try:
                if os.path.exists(src):
                    shutil.copy2(src, self.path)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        self.data[k] = v
        except Exception:
            pass
        for k, v in DEFAULTS.items():
            self.data.setdefault(k, v)

        # 迁移：老配置把登录口令明文写在 config.json 里，
        # 这里搬到系统加密区并从明文字段删除，避免口令随配置文件泄露。
        plain_pw = str(self.data.get("password", "") or "")
        if plain_pw:
            try:
                self.set_secret(plain_pw, "password")
                self.data.pop("password", None)
                self.save()
            except Exception:
                pass

        # 迁移：老版本默认走本地代理（10808），这里一律纠正为直连。
        # 否则用户不手动改设置就永远连不上服务器。
        if str(self.data.get("proxy_mode", "off") or "off").strip().lower() != "off":
            self.data["proxy_mode"] = "off"
            for k in ("proxy_url", "proxy_bypass_lan", "use_system_proxy"):
                self.data.pop(k, None)
            self.save()

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    # ------------------------------------------------------------ 私钥
    @property
    def _secrets(self):
        if not hasattr(self, "_sec"):
            from core.secret_store import SecretStore
            self._sec = SecretStore(self.dir)
        return self._sec

    def get_secret(self, name: str = "client_secret") -> str:
        """读一段密文（私钥 / 登录口令）。

        优先从系统加密区读；读不到再回退配置文件里的同名字段，
        保证"加密不可用"时功能不断，而不是直接失效。
        """
        v = self._secrets.load(name)
        if v:
            return v
        return str(self.data.get(name, "") or "")

    def set_secret(self, value: str, name: str = "client_secret") -> None:
        """写一段密文。能加密就加密存，不能则退回配置。"""
        self._secrets.clear(name)
        if value and self._secrets.save(value, name):
            self.data.pop(name, None)
        elif value:
            self.data[name] = value
        else:
            self.data.pop(name, None)
        self.save()

    def set(self, key: str, value: Any, autosave: bool = True) -> None:
        self.data[key] = value
        if autosave:
            self.save()

    def update(self, mapping: Dict[str, Any], autosave: bool = True) -> None:
        self.data.update(mapping)
        if autosave:
            self.save()

    # ------------------------------------------------------------ 便捷属性
    @property
    def server_url(self) -> str:
        u = str(self.get("server_url", "")).strip().rstrip("/")
        if u and not u.startswith(("http://", "https://")):
            u = "http://" + u
        return u

    @property
    def chunk_size(self) -> int:
        try:
            mb = int(self.get("chunk_size_mb", 4))
        except Exception:
            mb = 4
        return max(1, min(16, mb)) * 1024 * 1024

    @property
    def concurrency(self) -> int:
        try:
            c = int(self.get("concurrency", 4))
        except Exception:
            c = 4
        return max(1, min(16, c))


cfg = Config()
