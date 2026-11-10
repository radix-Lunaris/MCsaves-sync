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

"""服务端 HTTP 客户端（含重试、超时、统一异常）"""
from __future__ import annotations

import os
import time
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib.parse import quote, urlparse

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore


class MCSyncError(Exception):
    pass


import hashlib
import hmac
import ipaddress
import json as _json
import secrets
import socket
import time
import uuid

from core.config import cfg


AUTH_HINT = {
    "unauthorized": "服务器要求登录：请在设置里填写账号与口令后重新连接",
    "bad_credentials": "账号或口令错误",
    "disabled_user": "该账号已被管理员停用",
    "forbidden": "此操作需要管理员权限",
    "bad_token": "服务器口令错误",
    "unknown_client": "该客户端未在服务器登记，请重新配对（设置 → 重新配对）",
    "bad_sign": "客户端密钥校验失败，请重新配对（设置 → 服务器口令 → 重新配对）",
    "expired": "本机时间与服务器相差过大，请校准系统时间后重试",
    "replay": "请求被判定为重放，请重试",
    "ip_denied": "本机 IP 不在服务器白名单内",
    "locked": "因口令错误过多，服务器已临时锁定此 IP",
}


class AuthError(MCSyncError):
    pass


DEFAULT_PROXY_URL = "http://127.0.0.1:10808"


def host_is_private(host: str) -> bool:
    """判断目标是否为本机/内网地址（局域网服务器不该走代理）。"""
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "::1", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False          # 域名按公网处理
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def proxy_settings(base_url: str = "", mode=None, url=None, bypass_lan=None):
    """客户端一律直连，不经过任何代理。

    客户端连的是自己的服务器，不需要代理；系统里装的 Clash / v2ray 会设置
    HTTP_PROXY 等环境变量，requests 默认 trust_env=True 会把同步流量也丢给
    本地代理，代理没开或不稳定就报 "too many 503" / "无法连接代理服务器"。
    这里恒定返回直连，任何调用路径都不会走代理。
    真正需要代理的是 push.bat 推 GitHub，那是 git 的事，与此无关。
    """
    del base_url, mode, url, bypass_lan
    return False, {"http": "", "https": ""}, "直连（客户端不走代理）"


class Api:
    def __init__(self, base_url: str, timeout: float = 30.0, **kw):
        # kw 仅用于兼容旧调用点传入的 use_proxy，实际代理策略由 _apply_proxy 统一决定
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MCSync-Client/1.0"})
        # 客户端永远直连：不继承系统代理，也不使用任何显式代理。
        self.session.trust_env = False
        self.session.proxies = {"http": "", "https": ""}
        self.proxy_note = "直连（客户端不走代理）"
        # 客户端凭证（登录后写入配置，之后每次请求签名，不再传输口令）
        self.username = str(cfg.get("username", "") or "")
        self.password = str(cfg.get_secret("password") or "")
        self.token = ""                      # 已废弃：旧版服务器随机口令
        self.client_id = str(cfg.get("client_id", "") or "")
        self.client_secret = cfg.get_secret()
        self.paired_server = str(cfg.get("paired_server", "") or "")
        self.me: dict = {}                   # 登录后服务端返回的账号信息
        if Retry is not None:
            retry = Retry(
                total=5, connect=5, read=5, backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE", "HEAD"]),
                raise_on_status=False,
            )
            ad = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
            self.session.mount("http://", ad)
            self.session.mount("https://", ad)

    # ------------------------------------------------------------ 签名
    def _sign_headers(self, method: str, path: str, body: bytes) -> dict:
        """用客户端私钥对请求做 HMAC-SHA256 签名；未配对时返回空。"""
        if not self.client_id or not self.client_secret:
            return {}
        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        base = "\n".join([ts, nonce, method.upper(), path,
                           hashlib.sha256(body or b"").hexdigest()])
        sig = hmac.new(self.client_secret.encode("utf-8"), base.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return {"X-MCSync-Key": self.client_id, "X-MCSync-Ts": ts,
                "X-MCSync-Nonce": nonce, "X-MCSync-Sign": sig}

    def _apply_proxy(self) -> str:
        """客户端恒定直连（保留方法仅为兼容旧调用）。"""
        self.session.trust_env = False
        self.session.proxies = {"http": "", "https": ""}
        self.proxy_note = "直连（客户端不走代理）"
        return self.proxy_note

    def refresh_proxy(self) -> str:
        return self._apply_proxy()

    def _refresh_cred(self) -> None:
        self.username = str(cfg.get("username", "") or "")
        self.password = str(cfg.get_secret("password") or "")
        self.client_id = str(cfg.get("client_id", "") or "")
        self.client_secret = cfg.get_secret()
        self.paired_server = str(cfg.get("paired_server", "") or "")

    # ------------------------------------------------------------ 内部
    def _url(self, path: str) -> str:
        return self.base + path

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        if not self.base:
            raise MCSyncError("尚未配置服务器地址")
        kw.setdefault("timeout", self.timeout)
        # 自行序列化 JSON：保证签名覆盖的字节与实际发送的字节完全一致
        if "json" in kw:
            payload = kw.pop("json")
            body = _json.dumps(payload, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
            kw["data"] = body
            hdrs = dict(kw.pop("headers", {}) or {})
            hdrs["Content-Type"] = "application/json"
        else:
            body = kw.get("data") or b""
            if isinstance(body, str):
                body = body.encode("utf-8")
            hdrs = dict(kw.pop("headers", {}) or {})
        hdrs.update(self._sign_headers(method, path, body))
        kw["headers"] = hdrs
        try:
            r = self.session.request(method, self._url(path), **kw)
        # 顺序有讲究：ProxyError 是 ConnectionError 的子类，
        # 必须先捕获它，否则会被下面的 ConnectionError 吞掉，
        # 用户就只看到"无法连接服务器"而不知道其实是代理没开。
        except requests.exceptions.ProxyError as e:
            purl = str(cfg.get("proxy_url", DEFAULT_PROXY_URL) or "")
            raise MCSyncError(
                "连接被代理拦截：本机设置了系统代理，但客户端始终直连。"
                "请检查系统代理设置，或确认服务器地址与端口可达"
            ) from e
        except requests.exceptions.ConnectionError:
            raise MCSyncError(f"无法连接服务器 {self.base}（检查地址、端口与防火墙）")
        except requests.exceptions.Timeout:
            raise MCSyncError("请求超时，服务器响应过慢")
        except requests.exceptions.RequestException as e:
            raise MCSyncError(f"网络错误：{e}")
        if r.status_code >= 400:
            msg = ""
            try:
                j = r.json()
                msg = j.get("detail") or j.get("error") or str(j)
            except Exception:
                msg = r.text[:200]
            if r.status_code in (401, 403):
                raise AuthError(AUTH_HINT.get(str(msg), f"认证失败（{msg}）"))
            if r.status_code == 429:
                raise MCSyncError("服务器已临时锁定此 IP（口令错误过多），请稍后再试")
            raise MCSyncError(f"服务端返回 {r.status_code}：{msg}")
        return r

    # ------------------------------------------------------------ 配对
    def login(self, username: str = "", password: str = "", name: str = "") -> dict:
        """账号登录：校验账号口令，并把本机私钥登记到该账号名下。

        旧版本是填服务器随机口令——口令一人知道，全员可用，且无法区分是谁。
        现在改成账号：服务端据此判定角色（管理员 / 普通用户），
        也才能在管理面板里按人管理设备。
        """
        username = (username or cfg.get("username", "")).strip()
        password = password or cfg.get_secret("password")
        if not self.base:
            raise MCSyncError("尚未配置服务器地址")
        if not username or not password:
            raise AuthError("请填写账号与口令（由管理员在面板「账号管理」里创建）")
        cid = cfg.get("client_id") or str(uuid.uuid4())
        sec = cfg.get("client_secret") or secrets.token_hex(32)
        try:
            nm = name or (socket.gethostname() or "PC")[:32]
        except Exception:
            nm = "PC"
        res = self._json("POST", "/api/login", timeout=20,
                         json={"username": username, "password": password,
                               "client_id": cid, "secret": sec, "name": nm})
        cfg.set("username", username)
        cfg.set_secret(password, "password")
        cfg.set("client_id", cid)
        cfg.set_secret(sec)
        cfg.set("paired_server", self.base)
        self._refresh_cred()
        self.me = res.get("user") or {}
        return res

    def refresh_me(self) -> dict:
        """用已登记的私钥向服务端取回当前账号信息（含角色、显示名、停用状态）。

        为什么必须有这个方法：
          self.me 原先只在 login() 里赋值。而启动自动登录时，
          ensure_login() 发现"已配对"就直接返回、不再调用 login()，
          于是 self.me 一直是空字典 —— 顶栏把管理员显示成「普通用户」。
          同时管理员在面板改了角色/停用账号，客户端也一直拿不到最新状态。

        走 /api/me 而不是本地缓存：权限以服务端为准，改动即时生效。
        """
        try:
            res = self._json("GET", "/api/me", timeout=10)
        except Exception:
            return self.me
        u = res.get("user") or {}
        if u.get("username"):
            self.me = u
        return self.me

    def ensure_login(self, name: str = "") -> bool:
        """需要登录时自动登录一次；返回是否执行了登录。"""
        self._refresh_cred()
        if not self.username or not self.password:
            return False                      # 未配置账号 -> 服务端可能未开启鉴权
        if self.client_secret and self.paired_server == self.base:
            # 已登录且服务器未变：不重复登记私钥，但必须刷新一次身份。
            # 否则 self.me 为空，管理员会被显示成普通用户。
            self.refresh_me()
            return False
        self.login(self.username, self.password, name)
        return True

    # 兼容旧调用名
    def pair(self, token: str = "", name: str = "") -> dict:
        """旧接口保留：等价于用已保存的账号登录。"""
        del token
        return self.login(name=name)

    def ensure_paired(self, name: str = "") -> bool:
        return self.ensure_login(name)

    def _json(self, method: str, path: str, **kw):
        return self._req(method, path, **kw).json()

    # ------------------------------------------------------------ 接口
    def health(self) -> dict:
        return self._json("GET", "/api/health")

    def check_api_version(self) -> str:
        """比对服务端与客户端协议版本，返回提示（一致时为空）。"""
        try:
            from core.protocol import API_VERSION
            got = self.health().get("api_version")
        except Exception:
            return ""
        if got is None:
            return ""
        try:
            if int(got) != int(API_VERSION):
                return (f"服务端协议版本 {got}，客户端 {API_VERSION}，两者不一致。"
                        f"请升级客户端或服务端，否则可能出现费解的错误")
        except (TypeError, ValueError):
            return ""
        return ""

    def stats(self) -> dict:
        return self._json("GET", "/api/stats")

    def list_saves(self) -> List[dict]:
        return self._json("GET", "/api/saves").get("saves", [])

    def versions(self, name: str) -> List[dict]:
        return self._json("GET", f"/api/save/{quote(name)}/versions").get("versions", [])

    def plan(self, name: str, base_version: int, files: List[dict]) -> dict:
        return self._json(
            "POST", f"/api/save/{quote(name)}/plan",
            json={"base_version": int(base_version), "files": files},
            timeout=60,
        )

    def blob_check(self, hashes: List[str]) -> List[str]:
        if not hashes:
            return []
        r = self._json("POST", "/api/blob/check", json={"hashes": hashes}, timeout=60)
        return r.get("missing", [])

    def put_blob(self, h: str, data: bytes, size: Optional[int] = None) -> None:
        self._req(
            "PUT", f"/api/blob/{h}", data=data, timeout=120,
            headers={"Content-Type": "application/octet-stream", "X-Blob-Size": str(size if size is not None else len(data))},
        )

    def get_blob_to_file(self, h: str, dest: str, chunk: int = 1024 * 1024) -> int:
        """流式下载块到文件（原子写入）"""
        if not self.base:
            raise MCSyncError("尚未配置服务器地址")
        # 必须带签名头：开启鉴权后服务端会拒绝无凭证请求。
        # 旧版本这里直接用裸 session 发请求，导致"上传正常、下载报 401"。
        path = f"/api/blob/{h}"
        hdrs = self._sign_headers("GET", path, b"")
        tmp = dest + ".dl"
        written = 0
        try:
            with self.session.get(self._url(path), headers=hdrs, stream=True, timeout=120) as r:
                if r.status_code >= 400:
                    raise MCSyncError(f"下载数据块失败 {r.status_code}")
                with open(tmp, "wb") as f:
                    for buf in r.iter_content(chunk):
                        if not buf:
                            continue
                        f.write(buf)
                        written += len(buf)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return written

    def commit(self, name: str, base_version: int, files: List[dict], delete: List[str],
               note: str = "", instance: str = "", kind: str = "save") -> dict:
        return self._json(
            "POST", f"/api/save/{quote(name)}/commit",
            json={"base_version": int(base_version), "note": note, "files": files,
                  "delete": delete, "instance": instance, "kind": kind},
            timeout=180,
        )

    def manifest(self, name: str, vid: int) -> dict:
        return self._json("GET", f"/api/save/{quote(name)}/version/{vid}/manifest", timeout=120)

    def restore(self, name: str, vid: int) -> dict:
        return self._json("POST", f"/api/save/{quote(name)}/version/{vid}/restore")

    def delete_version(self, name: str, vid: int) -> dict:
        return self._json("DELETE", f"/api/save/{quote(name)}/version/{vid}")

    def delete_save(self, name: str) -> dict:
        return self._json("DELETE", f"/api/save/{quote(name)}")

    def set_instance(self, name: str, instance: str) -> dict:
        """手动指定云端存档所属整合包（老存档归类用）。"""
        return self._json("POST", f"/api/save/{quote(name)}/instance",
                          json={"instance": instance})

    def rename_save(self, name: str, new_name: str) -> dict:
        return self._json("POST", f"/api/save/{quote(name)}/rename", json={"new_name": new_name})

    def gc(self) -> dict:
        return self._json("POST", "/api/gc")

    def download_file(self, name: str, vid: int, rel: str, dest: str) -> int:
        if not self.base:
            raise MCSyncError("尚未配置服务器地址")
        path = f"/api/save/{quote(name)}/version/{vid}/file"
        url = self._url(path)
        tmp = dest + ".dl"
        written = 0
        try:
            # 必须带上签名头，否则开启鉴权后会被服务端拒绝。
            # 注意：查询串不参与签名（服务端校验只看 path），这里保持一致。
            hdrs = self._sign_headers("GET", path, b"")
            with self.session.get(url, params={"path": rel}, headers=hdrs,
                                  stream=True, timeout=180) as r:
                if r.status_code >= 400:
                    raise MCSyncError(f"下载文件失败 {r.status_code}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(tmp, "wb") as f:
                    for buf in r.iter_content(1024 * 1024):
                        if not buf:
                            continue
                        f.write(buf)
                        written += len(buf)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return written

    def zip_url(self, name: str, vid: int) -> str:
        return self._url(f"/api/save/{quote(name)}/version/{vid}/zip")

    # ------------------------------------------------------------ 连接测试
    def test(self) -> dict:
        t0 = time.time()
        h = self.health()
        h["ping_ms"] = int((time.time() - t0) * 1000)
        return h
