# -*- coding: utf-8 -*-
"""
客户端私钥的安全存放。

旧版把私钥明文写在 config.json 里。Windows 上改用 DPAPI
（CryptProtectData）加密后存本地文件——密钥与当前用户登录会话绑定，
换用户或拷到别的机器都解不开，别人直接拿配置文件也没用。
非 Windows 或 DPAPI 不可用时自动退回明文配置，保证功能不中断。
"""
from __future__ import annotations

import base64
import os
import sys

SERVICE = "MCSync"


def _win_protect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt = ctypes.windll.crypt32.CryptProtectData  # type: ignore[attr-defined]
        crypt.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p,
                          ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                          ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt.restype = wt.BOOL

        buf = ctypes.create_string_buffer(data, len(data))
        din = DATA_BLOB(len(data), buf)
        dout = DATA_BLOB()
        if not crypt(ctypes.byref(din), SERVICE, None, None, None, 0,
                     ctypes.byref(dout)):
            return None
        try:
            out = ctypes.string_at(dout.pbData, dout.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(dout.pbData)  # type: ignore[attr-defined]
        return out
    except Exception:
        return None


def _win_unprotect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        uncrypt = ctypes.windll.crypt32.CryptUnprotectData  # type: ignore[attr-defined]
        uncrypt.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
                            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB)]
        uncrypt.restype = wt.BOOL

        buf = ctypes.create_string_buffer(data, len(data))
        din = DATA_BLOB(len(data), buf)
        dout = DATA_BLOB()
        if not uncrypt(ctypes.byref(din), None, None, None, None, 0,
                       ctypes.byref(dout)):
            return None
        try:
            out = ctypes.string_at(dout.pbData, dout.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(dout.pbData)  # type: ignore[attr-defined]
        return out
    except Exception:
        return None


def _machine_key() -> bytes:
    """派生一台机器特有的密钥（非 Windows 平台用）。

    安全性说明：这不是强加密。它只能挡住"把配置文件拷走直接看"，
    挡不住在同一台机器上以同一用户身份运行的程序 —— 那种场景应该走
    DPAPI（Windows）。这里的目标是"绝不留明文"，而不是抗攻击。
    """
    import hashlib
    seeds = []
    try:
        # Linux: /var/lib/dbus/machine-id 或 /etc/machine-id
        for p in ("/var/lib/dbus/machine-id", "/etc/machine-id"):
            if os.path.exists(p):
                seeds.append(open(p, "r", encoding="utf-8", errors="ignore").read().strip())
                break
    except Exception:
        pass
    try:
        import getpass
        seeds.append(getpass.getuser())
    except Exception:
        pass
    seeds.append(os.path.expanduser("~"))
    return hashlib.sha256("|".join(seeds).encode("utf-8")).digest()


def _obfuscate(data: bytes) -> bytes:
    """机器密钥流加密：非 Windows 上的降级方案（避免明文落盘）。"""
    key = _machine_key()
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return b"O" + bytes(out)


def _deobfuscate(blob: bytes) -> bytes:
    if not blob:
        return b""
    key = _machine_key()
    data = blob[1:]
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return bytes(out)


class SecretStore:
    """按名字存放加密小片段（私钥、登录口令等）。

    每个名字一个文件，互不干扰；删一个不影响另一个。
    """

    def __init__(self, base_dir: str):
        self.dir = base_dir
        self.available = sys.platform == "win32"

    def _path(self, name: str) -> str:
        safe = "".join(ch for ch in (name or "default") if ch.isalnum() or ch in "-_")
        return os.path.join(self.dir, f"secret-{safe or 'default'}.bin")

    def save(self, value: str, name: str = "client_secret") -> bool:
        if not value:
            return False
        try:
            os.makedirs(self.dir, exist_ok=True)
            blob = _win_protect(value.encode("utf-8"))
            if blob is None:
                # 非 Windows：退化为机器密钥混淆，至少不留明文
                blob = _obfuscate(value.encode("utf-8"))
            with open(self._path(name), "wb") as f:
                f.write(base64.b64encode(blob))
            try:
                os.chmod(self._path(name), 0o600)
            except OSError:
                pass
            return True
        except Exception:
            return False

    def load(self, name: str = "client_secret") -> str:
        try:
            p = self._path(name)
            if not os.path.exists(p):
                return ""
            with open(p, "rb") as f:
                blob = base64.b64decode(f.read())
            raw = _win_unprotect(blob)
            if raw is None:
                if blob[:1] == b"O":
                    raw = _deobfuscate(blob)
                else:
                    return ""
            return raw.decode("utf-8")
        except Exception:
            return ""

    def clear(self, name: str = "client_secret") -> None:
        try:
            p = self._path(name)
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
