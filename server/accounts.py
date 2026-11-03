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
MCSync 账号系统

两级权限：
  admin —— 管理面板全部权限：管理云端存档（删/回滚/改名/归类/清理）与账号管理
  user  —— 客户端完整功能（上传/下载/版本管理）；面板仅可浏览

账号只能由管理员创建，没有自助注册，避免拿到地址的人自行进站。
首个管理员由「管理员密钥」（部署时生成的 16 位随机口令）引导创建，
之后一律账号密码登录。

口令存储：PBKDF2-HMAC-SHA256，每人一份随机盐，20 万次迭代。
不存明文，也不可逆。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Dict, List, Optional

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_USER)

PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 6
MAX_PASSWORD = 128


class AccountError(Exception):
    """账号操作失败（用户名重复、口令太短、角色非法等）"""


def _pbkdf2(password: str, salt: str, rounds: int = PBKDF2_ROUNDS) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
    return dk.hex()


def hash_password(password: str) -> tuple:
    """返回 (salt, hash)"""
    salt = secrets.token_hex(16)
    return salt, _pbkdf2(password, salt)


def verify_password(password: str, salt: str, expect: str) -> bool:
    if not salt or not expect:
        return False
    calc = _pbkdf2(password, salt)
    return hmac.compare_digest(calc, str(expect))


class AccountStore:
    """账号存储：JSON 文件，权限 0600。"""

    def __init__(self, data_dir: str):
        self.dir = os.path.abspath(data_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.file = os.path.join(self.dir, "accounts.json")
        self._lock = threading.RLock()
        self.users: Dict[str, dict] = self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> Dict[str, dict]:
        try:
            if os.path.exists(self.file):
                with open(self.file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                # 兼容两种写法：直接是 dict，或 {"users": {...}}
                if isinstance(d, dict):
                    if isinstance(d.get("users"), dict):
                        return d["users"]
                    return d
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        try:
            fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": self.users}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------------ 查询
    def has_any(self) -> bool:
        return bool(self.users)

    def get(self, username: str) -> Optional[dict]:
        u = (username or "").strip()
        if not u:
            return None
        return self.users.get(u)

    def count(self) -> int:
        return len(self.users)

    def list_users(self) -> List[dict]:
        """返回不含任何口令信息的安全视图"""
        out = []
        for name, u in sorted(self.users.items(), key=lambda kv: kv[0].lower()):
            out.append({
                "username": name,
                "display": u.get("display") or name,
                "role": u.get("role", ROLE_USER),
                "created_at": u.get("created_at", 0),
                "last_login": u.get("last_login", 0),
                "disabled": bool(u.get("disabled", False)),
            })
        return out

    def public(self, username: str) -> dict:
        u = self.get(username) or {}
        return {
            "username": (username or "").strip(),
            "display": u.get("display") or (username or "").strip(),
            "role": u.get("role", ROLE_USER),
            "disabled": bool(u.get("disabled", False)),
        }

    # ------------------------------------------------------------ 校验
    def check(self, username: str, password: str) -> Optional[dict]:
        """校验账号口令，成功返回账号记录，失败返回 None。"""
        u = self.get(username)
        if not u:
            return None
        if u.get("disabled"):
            return None
        if not verify_password(password or "", u.get("salt", ""), u.get("pwd", "")):
            return None
        return u

    def touch_login(self, username: str) -> None:
        with self._lock:
            u = self.users.get(username)
            if u:
                u["last_login"] = time.time()
                self._save()

    # ------------------------------------------------------------ 管理
    def create(self, username: str, password: str, role: str = ROLE_USER,
               display: str = "", by: str = "") -> dict:
        u = (username or "").strip()
        if not USERNAME_RE.match(u):
            raise AccountError("用户名需 3~32 位，仅允许字母、数字、下划线、点、连字符")
        if not password or len(password) < MIN_PASSWORD:
            raise AccountError(f"口令至少 {MIN_PASSWORD} 位")
        if len(password) > MAX_PASSWORD:
            raise AccountError(f"口令最多 {MAX_PASSWORD} 位")
        role = (role or ROLE_USER).strip().lower()
        if role not in VALID_ROLES:
            raise AccountError("角色必须是 admin 或 user")
        with self._lock:
            if u in self.users:
                raise AccountError(f"用户名「{u}」已存在")
            salt, pwd = hash_password(password)
            self.users[u] = {
                "salt": salt,
                "pwd": pwd,
                "role": role,
                "display": (display or "").strip() or u,
                "created_at": time.time(),
                "created_by": by or "",
                "last_login": 0,
                "disabled": False,
            }
            self._save()
            return self.public(u)

    def set_password(self, username: str, password: str) -> bool:
        if not password or len(password) < MIN_PASSWORD:
            raise AccountError(f"口令至少 {MIN_PASSWORD} 位")
        with self._lock:
            u = self.users.get((username or "").strip())
            if not u:
                return False
            salt, pwd = hash_password(password)
            u["salt"], u["pwd"] = salt, pwd
            self._save()
            return True

    def set_role(self, username: str, role: str) -> bool:
        role = (role or "").strip().lower()
        if role not in VALID_ROLES:
            raise AccountError("角色必须是 admin 或 user")
        with self._lock:
            u = self.users.get((username or "").strip())
            if not u:
                return False
            # 不允许把最后一个管理员降级，否则系统会失去管理能力
            if u.get("role") == ROLE_ADMIN and role != ROLE_ADMIN:
                admins = [k for k, v in self.users.items()
                          if v.get("role") == ROLE_ADMIN and not v.get("disabled")]
                if len(admins) <= 1:
                    raise AccountError("至少要保留一个管理员")
            u["role"] = role
            self._save()
            return True

    def set_disabled(self, username: str, disabled: bool) -> bool:
        with self._lock:
            u = self.users.get((username or "").strip())
            if not u:
                return False
            if disabled and u.get("role") == ROLE_ADMIN:
                admins = [k for k, v in self.users.items()
                          if v.get("role") == ROLE_ADMIN and not v.get("disabled")]
                if len(admins) <= 1:
                    raise AccountError("至少要保留一个可用管理员")
            u["disabled"] = bool(disabled)
            self._save()
            return True

    def delete(self, username: str) -> bool:
        with self._lock:
            u = (username or "").strip()
            if u not in self.users:
                return False
            if self.users[u].get("role") == ROLE_ADMIN:
                admins = [k for k, v in self.users.items()
                          if v.get("role") == ROLE_ADMIN and not v.get("disabled")]
                if len(admins) <= 1:
                    raise AccountError("至少要保留一个管理员")
            self.users.pop(u)
            self._save()
            return True
