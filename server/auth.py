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
MCSync 服务端安全层

提供四道防线：
  1) 配对口令   —— 16 位随机 Token，仅在「客户端配对 / Web 登录」时出示一次
  2) 客户端私钥 —— 配对成功后客户端用本地 32 字节密钥对每个请求做 HMAC-SHA256 签名，
                   之后不再传输任何口令，私钥永不上网
  3) 重放防护   —— 时间戳窗口 + 一次性 nonce
  4) 暴力防护   —— IP 失败计数锁定、IP 白名单、安全响应头

签名基串（\n 分隔）：
    ts \n nonce \n METHOD \n path \n sha256(body)
签名 = HMAC-SHA256(client_secret, 基串).hexdigest()
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import string
import threading
import time
from typing import Dict, Optional
from urllib.parse import quote, unquote

from accounts import (AccountError, AccountStore, ROLE_ADMIN,
                     ROLE_USER)


# ------------------------------------------------------------------ 常量

TOKEN_CHARS = string.ascii_letters + string.digits     # 62 种字符
TOKEN_LEN = 16
TS_WINDOW = 300          # 时间戳允许偏差（秒）
NONCE_TTL = 600          # nonce 记忆时长（秒）
FAIL_WINDOW = 600        # 失败计数窗口（秒）
FAIL_LIMIT = 10          # 窗口内失败次数上限
LOCK_SECONDS = 600
CROSS_FAIL_LIMIT = 60       # 同一 IP 跨账号探测的锁定阈值（远高于单账号，避免 NAT 误伤）       # 触发后锁定时长（秒）
SESSION_HOURS = 12


def gen_token(n: int = TOKEN_LEN) -> str:
    """生成 n 位强随机口令（大小写字母 + 数字）。"""
    return "".join(secrets.choice(TOKEN_CHARS) for _ in range(n))


def gen_secret() -> str:
    """客户端私钥：32 字节 -> 64 位十六进制。"""
    return secrets.token_hex(32)


def _hmac(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def path_variants(path: str) -> list:
    """签名基串里 path 的等价形式（去重、保序）。

    为什么需要：客户端按 HTTP 请求行签名，非 ASCII / 空格会带百分号编码，
    例如 /api/save/%E6%88%91.../versions；
    而 ASGI（uvicorn）交给应用的 scope["path"] 是**解码后**的
    /api/save/我的世界存档/versions。
    两边直接比对必然 bad_sign —— 于是「中文名或带空格的存档，点管理就报
    私钥验证失败」，而纯英文名的存档完全正常。
    这里把所有等价形式都试一遍，既能兼容老客户端，也能容忍中间代理的改写。
    """
    out = []

    def add(p):
        if p and p not in out:
            out.append(p)

    add(path)
    try:
        dec = unquote(path)
        add(dec)
        add(quote(dec, safe="/"))
        add(quote(path, safe="/"))
        # 部分代理会把 %20 还原成 +，或反之
        add(quote(dec, safe="/").replace("%20", "+"))
        add(dec.replace(" ", "+"))
    except Exception:
        pass
    return out


def const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a), str(b))


def client_ip(request) -> str:
    """取客户端真实 IP（可选信任反向代理的 X-Forwarded-For）。"""
    if os.environ.get("MCSYNC_TRUST_PROXY", "0") in ("1", "true", "yes"):
        xf = request.headers.get("x-forwarded-for", "")
        if xf:
            return xf.split(",")[0].strip()
    return (getattr(request.client, "host", "") or "") if request.client else ""


# ------------------------------------------------------------------ 主体

class Auth:
    def __init__(self, data_dir: str, token: Optional[str] = None,
                 enabled: bool = True, allow_ips: Optional[list] = None):
        self.dir = os.path.abspath(data_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.enabled = bool(enabled)
        self.allow_ips = [s for s in (allow_ips or []) if s]
        self._lock = threading.RLock()

        self.token_file = os.path.join(self.dir, "token.txt")
        self.clients_file = os.path.join(self.dir, "clients.json")

        self.token = (token or "").strip() or self._load_token() or gen_token()
        self._save_token()

        # 账号体系：客户端与面板统一用账号登录。
        # 上面那个随机口令降级为「管理员密钥」，仅用于创建首个管理员账号。
        self.accounts = AccountStore(self.dir)

        self.clients: Dict[str, dict] = self._load_clients()

        self._last_client_save = 0.0
        self._nonces: Dict[str, float] = {}
        self._fails: Dict[str, list] = {}
        self._locks: Dict[str, float] = {}

    # ------------------------------------------------------------ 口令
    def _load_token(self) -> str:
        try:
            if os.path.exists(self.token_file):
                with open(self.token_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except OSError:
            pass
        return ""

    def _save_token(self) -> None:
        try:
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.token)
        except OSError:
            pass

    def rotate_token(self) -> str:
        """轮换口令：使旧口令与所有 Web 会话立即失效（已配对客户端不受影响）。"""
        with self._lock:
            self.token = gen_token()
            self._save_token()
            self._locks.clear()
            self._fails.clear()
        return self.token

    def token_ok(self, given: str) -> bool:
        return bool(given) and const_eq(str(given), self.token)

    # ------------------------------------------------------------ 账号
    def needs_bootstrap(self) -> bool:
        """还没有任何账号时，需要用管理员密钥创建首个管理员。"""
        return not self.accounts.has_any()

    def bootstrap_admin(self, admin_key: str, username: str, password: str,
                        display: str = "") -> dict:
        """用管理员密钥创建首个管理员账号。"""
        if not self.token_ok(admin_key):
            raise ValueError("管理员密钥错误")
        if self.accounts.has_any():
            raise ValueError("账号系统已初始化，请直接用账号登录")
        return self.accounts.create(username, password, ROLE_ADMIN,
                                    display=display, by="bootstrap")

    def login_user(self, username: str, password: str):
        """校验账号口令。返回 (账号, 失败原因)。"""
        u = self.accounts.check(username, password)
        if not u:
            return None, "账号或口令错误"
        return u, None

    def touch_login(self, username: str) -> None:
        """记录最近登录时间（面板与客户端共用）。"""
        self.accounts.touch_login(username)

    def role_of_client(self, rec: dict) -> str:
        """查询某个客户端对应账号的当前角色（实时查，改权限立即生效）。"""
        name = (rec or {}).get("user", "")
        if not name:
            return ROLE_USER
        u = self.accounts.get(name)
        if not u:
            return ROLE_USER
        if u.get("disabled"):
            return ""
        return u.get("role", ROLE_USER)

    def role_of_session(self, username: str) -> str:
        u = self.accounts.get(username)
        if not u or u.get("disabled"):
            return ""
        return u.get("role", ROLE_USER)

    # ------------------------------------------------------------ 客户端
    def _load_clients(self) -> Dict[str, dict]:
        try:
            if os.path.exists(self.clients_file):
                with open(self.clients_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
        return {}

    def _save_clients(self) -> None:
        try:
            fd = os.open(self.clients_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.clients, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def register_client(self, username: str, client_id: str, secret: str,
                        name: str = "") -> dict:
        """账号登录后登记本机私钥。之后该客户端即可用私钥签名访问。

        旧版本是拿服务器随机口令配对，口令一旦泄露任何人都能接入；
        现在改为先验证账号口令，再把私钥挂到该账号名下，
        权限随账号角色走，管理员改权限即时生效。
        """
        acct = self.accounts.get(username)
        if not acct:
            raise ValueError("账号不存在")
        if acct.get("disabled"):
            raise ValueError("账号已被停用")
        if not client_id or len(client_id) > 64:
            raise ValueError("client_id 非法")
        if not secret or len(secret) < 32 or len(secret) > 256:
            raise ValueError("密钥非法")
        with self._lock:
            self.clients[client_id] = {
                "secret": secret,
                "name": name or client_id[:8],
                "user": username,
                "created_at": time.time(),
                "last_seen": time.time(),
            }
            self._save_clients()
        return {
            "client_id": client_id,
            "name": self.clients[client_id]["name"],
            "user": self.accounts.public(username),
        }

    def revoke(self, client_id: str) -> bool:
        with self._lock:
            if client_id in self.clients:
                self.clients.pop(client_id)
                self._save_clients()
                return True
        return False

    def client_list(self) -> list:
        """设备列表（含所属账号），供管理面板展示与撤销。"""
        with self._lock:
            return [
                {"client_id": k, "name": v.get("name", ""),
                 "user": v.get("user", ""),
                 "created_at": v.get("created_at", 0), "last_seen": v.get("last_seen", 0)}
                for k, v in self.clients.items()
            ]

    # ------------------------------------------------------------ 暴力防护
    def _lock_key(self, ip: str, tag: str = "") -> str:
        """锁定粒度。

        旧版只按 IP 锁定：同一家公司/校园网出口 IP 相同，
        一个人输错口令会把同网的所有人一起锁掉。
        登录接口传入账号名，于是按 (IP, 账号) 锁定；
        对未知账号的探测仍按 IP 计，但阈值高得多。
        """
        return f"{ip}|{tag}" if tag else ip

    def _locked(self, ip: str, tag: str = "") -> bool:
        if tag and self._locks.get(self._lock_key(ip, tag), 0) > time.time():
            return True
        until = self._locks.get(ip, 0)
        if until and time.time() < until:
            return True
        if until:
            self._locks.pop(ip, None)
        return False

    def record_fail(self, ip: str, tag: str = "") -> None:
        """记录一次失败。

        tag 非空（登录接口传账号名）时按 (IP, 账号) 计数并锁定，
        同时给该 IP 的跨账号探测计数，达到 CROSS_FAIL_LIMIT 才锁整个 IP。
        """
        now = time.time()
        with self._lock:
            key = self._lock_key(ip, tag)
            arr = [t for t in self._fails.get(key, []) if now - t <= FAIL_WINDOW]
            arr.append(now)
            self._fails[key] = arr
            if len(arr) >= FAIL_LIMIT:
                self._locks[key] = now + LOCK_SECONDS
                self._fails.pop(key, None)
            if tag:
                ck = f"cross:{ip}"
                carr = [t for t in self._fails.get(ck, []) if now - t <= FAIL_WINDOW]
                carr.append(now)
                self._fails[ck] = carr
                if len(carr) >= CROSS_FAIL_LIMIT:
                    self._locks[ip] = now + LOCK_SECONDS
                    self._fails.pop(ck, None)


    def _clear_fail(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def ip_allowed(self, ip: str) -> bool:
        if not self.allow_ips or not ip:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for rule in self.allow_ips:
            try:
                if "/" in rule:
                    if addr in ipaddress.ip_network(rule, strict=False):
                        return True
                elif ipaddress.ip_address(rule) == addr:
                    return True
            except ValueError:
                continue
        return False

    # ------------------------------------------------------------ 会话（Web）
    def make_session(self, username: str = "") -> str:
        """会话里带上账号名，这样管理员改权限/停用后能立即生效。"""
        exp = int(time.time()) + SESSION_HOURS * 3600
        u = (username or "").strip()
        sig = _hmac(self.token, f"{exp}.{u}")
        return f"{exp}.{u}.{sig}"

    def session_ok(self, val: str) -> bool:
        """仅校验签名与有效期；账号是否可用由 session_user 判断。"""
        if not val or val.count(".") < 2:
            return False
        exp, _, rest = val.partition(".")
        try:
            if int(exp) < time.time():
                return False
        except ValueError:
            return False
        user, _, sig = rest.rpartition(".")
        return const_eq(sig, _hmac(self.token, f"{exp}.{user}"))

    def session_user(self, val: str) -> str:
        if not self.session_ok(val):
            return ""
        exp, _, rest = val.partition(".")
        user, _, _sig = rest.rpartition(".")
        return user

    # ------------------------------------------------------------ 请求校验
    def verify(self, method: str, path: str, body: bytes, headers, ip: str = "") -> Optional[str]:
        """兼容旧签名：只返回失败原因。"""
        reason, _ctx = self.authenticate(method, path, body, headers, ip)
        return reason

    def authenticate(self, method: str, path: str, body: bytes, headers,
                     ip: str = "") -> tuple:
        """
        返回 (失败原因, 上下文)。失败原因为 None 表示通过。
        上下文含 user / role / via，供接口做权限判断。

        校验顺序：IP 白名单 -> 锁定 -> 管理员密钥 Bearer -> 客户端签名 -> Web 会话
        """
        ctx = {"user": "", "role": "", "via": ""}
        if not self.enabled:
            return None, ctx
        if not self.ip_allowed(ip):
            return "ip_denied", ctx
        if self._locked(ip):
            return "locked", ctx

        body = body or b""

        # 1) Bearer：管理员密钥（仅用于创建首个管理员，不用于日常操作）
        ah = headers.get("authorization", "")
        if ah.lower().startswith("bearer "):
            if self.token_ok(ah[7:].strip()):
                self._clear_fail(ip)
                ctx["via"] = "admin_key"
                ctx["role"] = ROLE_ADMIN
                return None, ctx
            self.record_fail(ip)
            return "bad_token", ctx

        # 2) 客户端私钥签名
        cid = headers.get("x-mcsync-key", "")
        ts = headers.get("x-mcsync-ts", "")
        nonce = headers.get("x-mcsync-nonce", "")
        sign = headers.get("x-mcsync-sign", "")
        if cid or sign:
            with self._lock:
                rec = self.clients.get(cid)
            if not rec:
                self.record_fail(ip)
                return "unknown_client", ctx
            role = self.role_of_client(rec)
            if not role:
                # 账号已被停用
                return "disabled_user", ctx
            try:
                ts_i = int(ts)
            except (TypeError, ValueError):
                self.record_fail(ip)
                return "bad_ts", ctx
            if abs(time.time() - ts_i) > TS_WINDOW:
                return "expired", ctx
            nk = f"{cid}:{nonce}"
            with self._lock:
                if nk in self._nonces:
                    return "replay", ctx
            body_sha = hashlib.sha256(body).hexdigest()
            # 逐个尝试 path 的等价形式，任一匹配即通过
            ok_sign = False
            for cand in path_variants(path):
                base = "\n".join([str(ts_i), nonce, method.upper(), cand, body_sha])
                if const_eq(sign, _hmac(rec["secret"], base)):
                    ok_sign = True
                    break
            if not ok_sign:
                self.record_fail(ip)
                return "bad_sign", ctx
            with self._lock:
                self._nonces[nk] = time.time()
                now = time.time()
                if len(self._nonces) > 20000:
                    self._nonces = {k: v for k, v in self._nonces.items() if now - v <= NONCE_TTL}
                rec["last_seen"] = now
                # 上传时每秒可能有上百个请求，落盘做节流，避免高频写盘
                if now - self._last_client_save > 30:
                    self._last_client_save = now
                    self._save_clients()
            self._clear_fail(ip)
            ctx["user"] = rec.get("user", "")
            ctx["role"] = role
            ctx["via"] = "client"
            return None, ctx

        # 3) Web 会话 Cookie
        cookie = headers.get("cookie", "")
        for part in cookie.split(";"):
            kv = part.strip()
            if kv.startswith("mcsync_session="):
                val = kv[len("mcsync_session="):]
                if self.session_ok(val):
                    user = self.session_user(val)
                    role = self.role_of_session(user)
                    if not role:
                        return "unauthorized", ctx
                    self._clear_fail(ip)
                    ctx["user"] = user
                    ctx["role"] = role
                    ctx["via"] = "web"
                    return None, ctx
        return "unauthorized", ctx
