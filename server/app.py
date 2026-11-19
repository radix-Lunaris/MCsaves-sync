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
MCSync 服务端 —— FastAPI 应用（免认证单空间模式）

环境变量：
  MCSYNC_DATA            数据目录，默认 /opt/mcsync/data（容器内 /data）
  MCSYNC_PORT            监听端口，默认 8650
  MCSYNC_HOST            监听地址，默认 0.0.0.0
  MCSYNC_VERIFY_BLOB     是否校验上传块哈希（1/0），默认 0（省 CPU）
  MCSYNC_KEEP_VERSIONS   每个存档保留的版本数，0=不限制
  MCSYNC_MAX_UPLOAD_MB   单次块上传上限（MB），默认 16
  MCSYNC_TOKEN           配对口令（16 位），留空则自动生成并保存到数据目录
  MCSYNC_AUTH            是否启用鉴权（1/0），默认 1
  MCSYNC_ALLOW_IPS       IP 白名单，逗号分隔，支持 CIDR，留空=不限制
  MCSYNC_TRUST_PROXY     是否信任 X-Forwarded-For（1/0），默认 0
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from protocol import CHUNK_SIZE, API_VERSION, normalize_save_name, norm_rel  # noqa: E402

# 服务端进程启动时刻：注入到管理页底部，便于确认页面是不是最新版本
_BOOT_TIME = time.strftime("%Y-%m-%d %H:%M")
from store import Store  # noqa: E402
from auth import Auth, client_ip  # noqa: E402

DATA_DIR = os.environ.get("MCSYNC_DATA", "/opt/mcsync/data")
VERIFY_BLOB = os.environ.get("MCSYNC_VERIFY_BLOB", "0") in ("1", "true", "True", "yes")
KEEP_VERSIONS = int(os.environ.get("MCSYNC_KEEP_VERSIONS", "0"))
MAX_UPLOAD_MB = int(os.environ.get("MCSYNC_MAX_UPLOAD_MB", "16"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

store = Store(DATA_DIR, verify_blob=VERIFY_BLOB, keep_versions=KEEP_VERSIONS)

# ------------------------------------------------------------------ 安全
AUTH_ENABLED = os.environ.get("MCSYNC_AUTH", "1") not in ("0", "false", "no", "False")
ALLOW_IPS = [x.strip() for x in os.environ.get("MCSYNC_ALLOW_IPS", "").split(",") if x.strip()]
auth = Auth(DATA_DIR, token=os.environ.get("MCSYNC_TOKEN", ""),
            enabled=AUTH_ENABLED, allow_ips=ALLOW_IPS)

# 无需鉴权即可访问的接口（health 供客户端探测；登录与引导接口自身校验凭证）
PUBLIC_API = {"/api/health", "/api/login", "/api/web/login", "/api/web/logout",
              "/api/bootstrap", "/api/bootstrap/status"}

# 只有管理员能做的操作。
# 原则：客户端同步（上传/下载/读版本）普通用户全部可用；
#       「改变云端既有数据」或「动账号」的操作收归管理员。
def _needs_admin(method: str, path: str) -> bool:
    m = method.upper()
    if path.startswith("/api/admin/") or path.startswith("/api/security"):
        return True
    # 删存档 / 删版本 / 回滚 / 改名 / 归类 / 回收
    if m == "DELETE" and (path.startswith("/api/save/") or path == "/api/gc"):
        return True
    if m == "POST" and (
            "/restore" in path or "/rename" in path or path.endswith("/instance")
            or path == "/api/gc"):
        return True
    return False

def _require_admin(request: Request):
    if getattr(request.state, "role", "") != "admin":
        raise HTTPException(403, "需要管理员权限")


START_TIME = time.time()

app = FastAPI(
    title="MCSync Server", version="1.0.0",
    # 启用鉴权时关闭交互式文档，避免接口结构外泄
    docs_url=None if AUTH_ENABLED else "/api/docs",
    openapi_url=None if AUTH_ENABLED else "/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """鉴权 + 安全响应头。"""
    path = request.url.path
    if auth.enabled and path.startswith("/api/") and path not in PUBLIC_API:
        body = await request.body() if request.method in ("POST", "PUT", "PATCH", "DELETE") else b""
        reason, ctx = auth.authenticate(request.method, path, body,
                                        request.headers, client_ip(request))
        if reason:
            code = 429 if reason == "locked" else (403 if reason == "ip_denied" else 401)
            return JSONResponse({"error": reason}, status_code=code)
        if _needs_admin(request.method, path) and ctx.get("role") != "admin":
            return JSONResponse({"error": "forbidden",
                                 "detail": "此操作需要管理员权限"}, status_code=403)
        # 把身份塞进 request.state，供各接口记录"谁做的"
        request.state.user = ctx.get("user", "")
        request.state.role = ctx.get("role", "")
    resp = await call_next(request)
    for k, v in (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                 ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")):
        resp.headers.setdefault(k, v)
    return resp


# ------------------------------------------------------------------ 模型

class FileMeta(BaseModel):
    path: str
    size: int
    mtime: float = 0
    hash: str


class LoginRequest(BaseModel):
    """客户端登录：账号口令 + 本机私钥（登录即登记私钥）"""
    username: str = ""
    password: str = ""
    client_id: str = ""
    secret: str = ""
    name: str = ""


class WebLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class BootstrapRequest(BaseModel):
    """用管理员密钥创建首个管理员账号"""
    admin_key: str = ""
    username: str = ""
    password: str = ""
    display: str = ""


class AccountCreateRequest(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "user"
    display: str = ""


class PasswordRequest(BaseModel):
    password: str = ""


class RoleRequest(BaseModel):
    role: str = "user"


class DisableRequest(BaseModel):
    disabled: bool = True


class SetInstanceRequest(BaseModel):
    instance: str = ""


class PlanRequest(BaseModel):
    base_version: int = 0
    files: List[FileMeta] = Field(default_factory=list)


class BlobCheckRequest(BaseModel):
    hashes: List[str] = Field(default_factory=list)


class CommitRequest(BaseModel):
    instance: str = ""
    base_version: int = 0
    note: str = ""
    files: List[Dict[str, Any]] = Field(default_factory=list)
    delete: List[str] = Field(default_factory=list)  # 需要从云端移除的相对路径
    kind: str = "save"        # save=存档 / config=整合包配置


# ------------------------------------------------------------------ 账号 / 登录

@app.get("/api/bootstrap/status")
def bootstrap_status():
    """面板用来判断该显示「创建管理员」还是「登录」。"""
    return {"initialized": not auth.needs_bootstrap()}


@app.post("/api/bootstrap")
def bootstrap(req: BootstrapRequest):
    """用管理员密钥创建首个管理员账号。"""
    if not auth.enabled:
        raise HTTPException(400, "服务端未启用鉴权")
    try:
        u = auth.bootstrap_admin(req.admin_key or "", req.username or "",
                                 req.password or "", display=req.display or "")
    except ValueError as e:
        raise HTTPException(403, str(e))
    except Exception as e:                      # accounts.AccountError
        raise HTTPException(400, str(e))
    return {"ok": True, "user": u}


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    """客户端登录：校验账号口令，并把本机私钥登记到该账号名下。"""
    if not auth.enabled:
        return {"ok": True, "user": {"username": "", "role": "admin", "display": ""}}
    ip = client_ip(request)
    if auth._locked(ip, tag=(getattr(req, "username", "") or "").strip().lower()):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    acct, why = auth.login_user(req.username or "", req.password or "")
    if not acct:
        # 登录失败也要计入暴力防护，否则可以无限撞库
        auth.record_fail(ip, tag=(req.username or "").strip().lower())
        raise HTTPException(401, {"error": "bad_credentials", "detail": why})
    cid = (req.client_id or "").strip()
    sec = (req.secret or "").strip()
    if not cid or not sec:
        raise HTTPException(400, "缺少 client_id / secret")
    try:
        res = auth.register_client((req.username or "").strip(), cid, sec,
                                   name=req.name or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.touch_login((req.username or "").strip())
    return {"ok": True, **res}


@app.post("/api/web/login")
def web_login(req: WebLoginRequest, request: Request, response: Response):
    """面板登录：校验通过后下发会话 Cookie。"""
    if not auth.enabled:
        return {"ok": True, "user": {"username": "", "role": "admin", "display": ""}}
    ip = client_ip(request)
    if auth._locked(ip, tag=(getattr(req, "username", "") or "").strip().lower()):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    acct, why = auth.login_user(req.username or "", req.password or "")
    if not acct:
        auth.record_fail(ip, tag=(req.username or "").strip().lower())
        raise HTTPException(401, {"error": "bad_credentials", "detail": why})
    auth._clear_fail(ip)
    user = (req.username or "").strip()
    auth.touch_login(user)
    val = auth.make_session(user)
    response.set_cookie(
        "mcsync_session", val, httponly=True, samesite="lax",
        max_age=12 * 3600, path="/")
    return {"ok": True, "user": auth.accounts.public(user)}


@app.post("/api/web/logout")
def web_logout(response: Response):
    response.delete_cookie("mcsync_session", path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = getattr(request.state, "user", "")
    role = getattr(request.state, "role", "")
    if not user:
        return {"user": {"username": "", "role": role, "display": ""}}
    return {"user": auth.accounts.public(user)}


# ------------------------------------------------------------------ 账号管理（管理员）

@app.get("/api/admin/accounts")
def admin_list_accounts(request: Request):
    _require_admin(request)
    users = auth.accounts.list_users()
    # 附带每个账号登记了几台设备
    for u in users:
        u["devices"] = sum(
            1 for c in auth.clients.values() if c.get("user") == u["username"])
    return {"accounts": users}


@app.post("/api/admin/accounts")
def admin_create_account(req: AccountCreateRequest, request: Request):
    _require_admin(request)
    try:
        u = auth.accounts.create(req.username or "", req.password or "",
                                 role=req.role or "user", display=req.display or "",
                                 by=getattr(request.state, "user", ""))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "account": u}


@app.post("/api/admin/accounts/{name}/password")
def admin_reset_password(name: str, req: PasswordRequest, request: Request):
    _require_admin(request)
    if not auth.accounts.get(name):
        raise HTTPException(404, "账号不存在")
    try:
        auth.accounts.set_password(name, req.password or "")
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/role")
def admin_set_role(name: str, req: RoleRequest, request: Request):
    _require_admin(request)
    if not auth.accounts.get(name):
        raise HTTPException(404, "账号不存在")
    try:
        auth.accounts.set_role(name, req.role or "user")
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "account": auth.accounts.public(name)}


@app.post("/api/admin/accounts/{name}/disabled")
def admin_set_disabled(name: str, req: DisableRequest, request: Request):
    _require_admin(request)
    if not auth.accounts.get(name):
        raise HTTPException(404, "账号不存在")
    try:
        auth.accounts.set_disabled(name, bool(req.disabled))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "account": auth.accounts.public(name)}


@app.delete("/api/admin/accounts/{name}")
def admin_delete_account(name: str, request: Request):
    _require_admin(request)
    try:
        okd = auth.accounts.delete(name)
    except Exception as e:
        raise HTTPException(400, str(e))
    if not okd:
        raise HTTPException(404, "账号不存在")
    # 同时踢掉该账号下所有已登记设备
    for cid, rec in list(auth.clients.items()):
        if rec.get("user") == name:
            auth.clients.pop(cid, None)
    auth._save_clients()
    return {"ok": True}


# ------------------------------------------------------------------ 回收站 / 审计（管理员）

@app.get("/api/admin/trash")
def admin_list_trash(request: Request):
    _require_admin(request)
    return {"trash": store.list_trash()}


@app.post("/api/admin/trash/{name}/restore")
def admin_restore_trash(name: str, request: Request):
    _require_admin(request)
    if not store.restore_trash(name, getattr(request.state, "user", "")):
        raise HTTPException(404, "回收站里没有该存档")
    return {"ok": True}


@app.delete("/api/admin/trash/{name}")
def admin_purge_one(name: str, request: Request):
    """彻底删除（不可恢复）"""
    _require_admin(request)
    store.delete_save(name, actor=getattr(request.state, "user", ""), hard=True)
    with store._lock:
        store._conn.execute("DELETE FROM trash WHERE name=?", (name,))
        store._conn.commit()
    return {"ok": True}


@app.post("/api/admin/trash/purge")
def admin_purge_expired(request: Request, days: int = 7):
    """清理回收站里超过 N 天的条目（默认 7 天）"""
    _require_admin(request)
    n = store.purge_trash(days)
    return {"ok": True, "purged": n}


@app.get("/api/admin/audit")
def admin_list_audit(request: Request, limit: int = 200):
    _require_admin(request)
    return {"audit": store.list_audit(limit)}


# ------------------------------------------------------------------ 我的设备

@app.get("/api/me/devices")
def my_devices(request: Request):
    """普通用户也能看自己登记了哪些机器，并可自助踢掉。"""
    me = getattr(request.state, "user", "")
    if not me:
        raise HTTPException(401, "未登录")
    out = []
    for cid, v in auth.clients.items():
        if v.get("user") == me:
            out.append({"client_id": cid, "name": v.get("name", ""),
                        "created_at": v.get("created_at", 0),
                        "last_seen": v.get("last_seen", 0)})
    return {"devices": out}


@app.delete("/api/me/devices/{cid}")
def revoke_my_device(cid: str, request: Request):
    me = getattr(request.state, "user", "")
    if not me:
        raise HTTPException(401, "未登录")
    rec = auth.clients.get(cid)
    if not rec or rec.get("user") != me:
        raise HTTPException(404, "设备不存在或不属于你")
    auth.revoke(cid)
    return {"ok": True}


# ------------------------------------------------------------------ 基础

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": "mcsync",
        "api_version": API_VERSION,
        "chunk_size": CHUNK_SIZE,
        "uptime": int(time.time() - START_TIME),
        "verify_blob": VERIFY_BLOB,
        "keep_versions": KEEP_VERSIONS,
    }


@app.get("/api/stats")
def stats():
    s = store.stats()
    s["uptime"] = int(time.time() - START_TIME)
    s["data_dir"] = DATA_DIR
    return s


@app.get("/api/saves")
def list_saves():
    return {"saves": store.list_saves_with_users()}



@app.get("/api/security")
def security_info():
    return {
        "auth_enabled": auth.enabled,
        "token_len": len(auth.token),
        "allow_ips": auth.allow_ips,
        "clients": auth.client_list(),
    }


@app.delete("/api/security/clients/{cid}")
def revoke_client(cid: str):
    if not auth.revoke(cid):
        raise HTTPException(404, "客户端不存在")
    return {"ok": True}


# ------------------------------------------------------------------ 同步计划

@app.post("/api/save/{name}/plan")
def plan(name: str, req: PlanRequest):
    """
    上传前的差异计算：
      need_upload   : 内容需要真正上传的文件（服务端没有对应块）
      up_to_date    : 云端已完全一致的文件
      cloud_newer   : 云端更新但本地未改（下载方向会用到）
      conflicts     : 本地与云端相对基准版本都改过
      to_delete     : 本地已删除、云端应同步删除的路径
      cloud_only    : 云端有而本地没有且非删除的路径
    """
    name = normalize_save_name(name)
    local = {}
    for f in req.files:
        local[norm_rel(f.path)] = f

    latest = store.latest_version(name)
    cloud: Dict[str, dict] = {}
    if latest:
        m = store.load_manifest(name, latest["id"]) or {}
        cloud = m.get("files", {})

    base: Dict[str, dict] = {}
    if req.base_version and req.base_version != (latest["id"] if latest else 0):
        bm = store.load_manifest(name, req.base_version)
        if bm:
            base = bm.get("files", {})

    need_upload: List[str] = []
    up_to_date: List[str] = []
    cloud_newer: List[str] = []
    conflicts: List[dict] = []
    instant: List[str] = []

    for path, f in local.items():
        c = cloud.get(path)
        if c is None:
            if store.has_blob(f.hash):
                instant.append(path)
            else:
                need_upload.append(path)
            continue
        if c.get("hash") == f.hash:
            up_to_date.append(path)
            continue
        b = base.get(path)
        base_hash = b.get("hash") if b else None
        if base_hash == f.hash:
            cloud_newer.append(path)          # 本地没动，云端动了
        elif base_hash == c.get("hash") or base_hash is None:
            # 云端没动（或无基准），本地动了 -> 上传
            if store.has_blob(f.hash):
                instant.append(path)
            else:
                need_upload.append(path)
        else:
            conflicts.append(
                {"path": path, "local_hash": f.hash, "cloud_hash": c.get("hash"), "base_hash": base_hash}
            )

    to_delete: List[str] = []
    cloud_only: List[str] = []
    for path, c in cloud.items():
        if path in local:
            continue
        b = base.get(path)
        if b is not None and b.get("hash") == c.get("hash"):
            to_delete.append(path)             # 本地删了，云端跟着删
        else:
            cloud_only.append(path)

    return {
        "save": name,
        "latest_version": latest["id"] if latest else 0,
        "base_version": req.base_version,
        "has_cloud": latest is not None,
        "need_upload": sorted(need_upload),
        "instant": sorted(instant),
        "up_to_date": sorted(up_to_date),
        "cloud_newer": sorted(cloud_newer),
        "conflicts": conflicts,
        "to_delete": sorted(to_delete),
        "cloud_only": sorted(cloud_only),
    }


@app.post("/api/blob/check")
def blob_check(req: BlobCheckRequest):
    missing = store.missing_blobs(req.hashes)
    return {"missing": missing, "present": len(req.hashes) - len(missing)}


@app.put("/api/blob/{hash}")
async def put_blob(hash: str, request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "空数据块")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"块过大（上限 {MAX_UPLOAD_MB} MB）")
    declared = request.headers.get("X-Blob-Size")
    size = int(declared) if declared and declared.isdigit() else None
    try:
        store.put_blob(hash, data, size)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "hash": hash, "size": len(data)}


@app.get("/api/blob/{hash}")
def get_blob(hash: str, request: Request):
    p = store.read_blob(hash)
    if not p:
        raise HTTPException(404, "块不存在")
    return FileResponse(p, media_type="application/octet-stream", filename=hash)


# ------------------------------------------------------------------ 提交版本

@app.post("/api/save/{name}/commit")
def commit(name: str, req: CommitRequest, request: Request):
    name = normalize_save_name(name)
    files: Dict[str, dict] = {}
    missing_chunks: List[str] = []
    for f in req.files:
        rel = norm_rel(f.get("path", ""))
        if not rel:
            continue
        pieces = [[int(x[0]), int(x[1]), str(x[2])]
                  for x in (f.get("pieces") or []) if len(x) >= 3]
        chunks = list(f.get("chunks") or [])
        fsize = int(f.get("size", 0) or 0)

        # ── 防撑爆闸门 ──
        # 曾经客户端的 region 分片算法有缺陷：分片严重重叠，
        # 400MB 的存档被切成 16GB 的分片上传，直接把服务器磁盘写满。
        # 分片是有序且不重叠的，其总长必须恰好等于文件大小；
        # 这里在落盘前拒掉异常清单，宁可让上传失败，也不让磁盘被撑爆。
        if pieces and fsize > 0:
            _psum = sum(p[1] for p in pieces)
            if _psum > fsize:
                raise HTTPException(
                    400,
                    f"清单异常：文件 {rel} 声明 {fsize} 字节，"
                    f"但分片总长 {_psum} 字节（{_psum / max(fsize, 1):.1f} 倍）。"
                    f"请升级客户端后重试。")

        if not pieces:
            if not chunks and fsize > 0:
                chunks = [f.get("hash")]   # 空文件不补整文件哈希，它没有数据块
        # 新清单按分片校验，旧清单退化为顺序块
        for h in ([p[2] for p in pieces] if pieces else chunks):
            if not store.has_blob(h):
                missing_chunks.append(h)
        files[rel] = {
            "path": rel,
            "size": int(f.get("size", 0)),
            "mtime": float(f.get("mtime", 0)),
            "hash": f.get("hash", ""),
            "chunks": chunks,
            "pieces": pieces,
            "type": f.get("type", "") or "",
        }
    if missing_chunks:
        raise HTTPException(
            409,
            {"error": "missing_chunks", "count": len(missing_chunks), "sample": missing_chunks[:5]},
        )

    # 云端已存在但本次清单没有、且未被显式删除的路径 -> 保留（避免误删）
    latest = store.latest_version(name)
    if latest:
        m = store.load_manifest(name, latest["id"]) or {}
        for rel, cf in m.get("files", {}).items():
            if rel not in files and rel not in {norm_rel(d) for d in req.delete}:
                files[rel] = cf

    if not files:
        raise HTTPException(400, "空存档，拒绝创建版本")

    parent = latest["id"] if latest else 0
    v = store.create_version(name, files, note=req.note or "", parent=parent,
                             instance=getattr(req, "instance", "") or "",
                             user=getattr(request.state, "user", ""),
                             kind=(getattr(req, "kind", "") or "save"))
    return {"ok": True, "version": v, "file_count": len(files)}


@app.post("/api/save/{name}/instance")
def set_instance(name: str, req: SetInstanceRequest):
    """手动指定云端存档所属整合包（老存档归类用）。"""
    name = normalize_save_name(name)
    ok = store.set_instance(name, req.instance or "")
    if not ok:
        raise HTTPException(404, "云端存档不存在")
    return {"ok": True, "name": name, "instance": (req.instance or "").strip()}


@app.get("/api/save/{name}/versions")
def versions(name: str):
    name = normalize_save_name(name)
    return {"save": name, "versions": store.list_versions(name)}


@app.get("/api/save/{name}/version/{vid}/manifest")
def manifest(name: str, vid: int):
    name = normalize_save_name(name)
    m = store.load_manifest(name, vid)
    if not m:
        raise HTTPException(404, "版本不存在")
    return m


@app.get("/api/save/{name}/version/{vid}/file")
def download_file(name: str, vid: int, path: str):
    name = normalize_save_name(name)
    m = store.load_manifest(name, vid)
    if not m:
        raise HTTPException(404, "版本不存在")
    rel = norm_rel(path)
    f = m.get("files", {}).get(rel)
    if not f:
        raise HTTPException(404, "文件不存在")

    def gen():
        hs = f.get("chunks") or ([] if int(f.get("size", 0) or 0) <= 0 else [f.get("hash")])
        for h in hs:
            p = store.read_blob(h)
            if not p:
                raise HTTPException(410, f"数据块缺失 {h}")
            with open(p, "rb") as fh:
                while True:
                    buf = fh.read(1024 * 1024)
                    if not buf:
                        break
                    yield buf

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(rel)}"'},
    )


@app.get("/api/save/{name}/version/{vid}/zip")
def download_zip(name: str, vid: int):
    name = normalize_save_name(name)
    zp = store.make_zip(name, vid)

    def gen():
        try:
            with open(zp, "rb") as fh:
                while True:
                    buf = fh.read(1024 * 1024)
                    if not buf:
                        break
                    yield buf
        finally:
            try:
                os.remove(zp)
            except OSError:
                pass

    return StreamingResponse(
        gen(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}-v{vid}.zip"'},
    )


@app.post("/api/save/{name}/version/{vid}/restore")
def restore(name: str, vid: int):
    name = normalize_save_name(name)
    try:
        v = store.restore_version(name, vid)
    except KeyError:
        raise HTTPException(404, "版本不存在")
    return {"ok": True, "version": v}


@app.delete("/api/save/{name}/version/{vid}")
def delete_version(name: str, vid: int):
    name = normalize_save_name(name)
    if not store.delete_version(name, vid):
        raise HTTPException(404, "版本不存在")
    return {"ok": True}


@app.delete("/api/save/{name}")
def delete_save(name: str, request: Request):
    name = normalize_save_name(name)
    okd = store.delete_save(name, actor=getattr(request.state, "user", ""))
    if not okd:
        raise HTTPException(404, "存档不存在")
    return {"ok": True, "trashed": True}
    return {"ok": True}


class RenameRequest(BaseModel):
    new_name: str


@app.post("/api/save/{name}/rename")
def rename_save(name: str, req: RenameRequest):
    name = normalize_save_name(name)
    if not store.rename_save(name, req.new_name):
        raise HTTPException(404, "存档不存在")
    return {"ok": True, "name": normalize_save_name(req.new_name)}


@app.post("/api/gc")
def gc():
    return {"ok": True, **store.gc()}


# ------------------------------------------------------------------ Web 管理页

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _render_index() -> HTMLResponse:
        """返回管理页面。

        两个关键点：
        1. 必须禁用缓存。重新部署后若浏览器还拿着旧页面，
           旧页面可能请求已经变更的接口，表现为"打开就一直转圈"，
           而用户根本想不到是缓存 —— 这类问题极难自查。
        2. 注入构建标识。页面底部会显示它，用户一眼能确认
           自己看的是不是最新版本。
        """
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        try:
            build = "API v%s · 启动于 %s" % (API_VERSION, _BOOT_TIME)
        except Exception:
            build = ""
        html = html.replace("__BUILD__", build)
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/web", response_class=HTMLResponse)
    def index():
        return _render_index()


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not_found", "path": request.url.path}, status_code=404)
    if os.path.isdir(STATIC_DIR) and os.path.exists(os.path.join(STATIC_DIR, "index.html")):
        r = _render_index()
        r.status_code = 200
        return r
    return JSONResponse({"error": "not_found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("MCSYNC_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCSYNC_PORT", "8650")),
        log_level="info",
    )
