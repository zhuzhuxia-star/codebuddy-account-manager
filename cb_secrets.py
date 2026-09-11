# -*- coding: utf-8 -*-
"""
CodeBuddy secret storage 加解密 / state.vscdb 读写（Windows / DPAPI）。

已验证的格式（本机实测通过）：
- 主密钥：`%APPDATA%\\CodeBuddy\\Local State` -> os_crypt.encrypted_key
  （base64，去掉 5 字节 "DPAPI" 头后用 CryptUnprotectData 解出 32 字节 AES-256 密钥）
- 条目：`v10`(3B) + IV(12B) + ciphertext + authTag(16B)，AES-256-GCM
- value 存于 SQLite 表 ItemTable(key, value)，value 为 JSON: {"data":[...], "type":"Buffer"}
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sqlite3
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None

EXTENSION_ID = "tencent-cloud.coding-copilot"

# 登录态条目 key 名因版本而异（旧版 accessToken / 国内版 accessTokencn），
# 均按特征识别，不写死。
CANDIDATE_DIRS = ("CodeBuddy CN", "CodeBuddy")


def _detect_app_dir() -> Path:
    """自动定位真实 CodeBuddy 用户数据目录（含 Local State + state.vscdb，取 DB 最新者）。"""
    roam = Path(os.environ.get("APPDATA", ""))
    best, best_mtime = None, -1.0
    for name in CANDIDATE_DIRS:
        d = roam / name
        db = d / "User" / "globalStorage" / "state.vscdb"
        if db.exists() and (d / "Local State").exists():
            mt = db.stat().st_mtime
            if mt > best_mtime:
                best, best_mtime = d, mt
    if best is None:
        raise SecretError(
            "未找到 CodeBuddy 数据目录（检查 %APPDATA% 下是否存在 "
            "'CodeBuddy CN' 或 'CodeBuddy'，且含 Local State 与 state.vscdb）")
    return best


APP_DIR = _detect_app_dir()
LOCAL_STATE = APP_DIR / "Local State"
STATE_DB = APP_DIR / "User" / "globalStorage" / "state.vscdb"


class SecretError(RuntimeError):
    pass


# ---------- DPAPI ----------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", wt.LPVOID)]


_crypt32 = ctypes.windll.crypt32
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32 = ctypes.windll.kernel32
_kernel32.LocalFree.argtypes = [wt.HLOCAL]
_kernel32.LocalFree.restype = wt.HLOCAL

CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, wt.LPVOID))


def dpapi_protect(data: bytes) -> bytes:
    pIn = _blob(data)
    pOut = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(pIn), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(pOut))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(pOut.pbData, pOut.cbData)
    finally:
        _kernel32.LocalFree(pOut.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    pIn = _blob(data)
    pOut = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(pIn), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(pOut))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(pOut.pbData, pOut.cbData)
    finally:
        _kernel32.LocalFree(pOut.pbData)


# ---------- AES 主密钥 ----------
def aes_key_from(local_state: Path) -> bytes:
    """从指定 Local State 解出 32 字节 AES 密钥（供 CodeBuddy 之外的同类客户端复用）。"""
    if not local_state.exists():
        raise SecretError(f"未找到 {local_state}")
    try:
        ls = json.loads(local_state.read_text(encoding="utf-8"))
        enc = ls["os_crypt"]["encrypted_key"]
    except (KeyError, json.JSONDecodeError) as e:
        raise SecretError(f"解析 Local State 失败: {e}") from e
    blob = base64.b64decode(enc)
    if not blob.startswith(b"DPAPI"):
        raise SecretError("encrypted_key 不是 DPAPI 格式")
    key = dpapi_unprotect(blob[5:])
    if len(key) != 32:
        raise SecretError(f"AES key 长度异常: {len(key)}")
    return key


def get_aes_key() -> bytes:
    """从 Local State 解出本机 32 字节 AES 密钥。"""
    return aes_key_from(LOCAL_STATE)


def find_state_db(root: Path) -> Path | None:
    """在客户端数据目录下定位 state.vscdb（VS Code 系布局，找不到则向下找一层）。"""
    direct = root / "User" / "globalStorage" / "state.vscdb"
    if direct.exists():
        return direct
    try:
        for i, p in enumerate(root.rglob("state.vscdb")):
            if i >= 200:
                break
            return p
    except Exception:
        pass
    return None


# ---------- v10 加解密 ----------
def decrypt_v10(data: bytes, key: bytes) -> str:
    """解密一条 v10 密文 -> 明文文本。"""
    if AESGCM is None:
        raise SecretError("缺少 cryptography 库")
    if not data.startswith(b"v10"):
        raise SecretError("不是 v10 格式")
    iv, ct = data[3:15], data[15:]
    tag, body = ct[-16:], ct[:-16]
    return AESGCM(key).decrypt(iv, body + tag, None).decode("utf-8")


def encrypt_v10(plain: str, key: bytes) -> bytes:
    """明文文本 -> v10 密文（每次随机 IV）。"""
    if AESGCM is None:
        raise SecretError("缺少 cryptography 库")
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
    ct, tag = sealed[:-16], sealed[-16:]
    return b"v10" + iv + ct + tag


def to_value_json(raw: bytes) -> str:
    return json.dumps({"data": list(raw), "type": "Buffer"}, separators=(",", ":"))


def from_value_json(s: str) -> bytes:
    return bytes(json.loads(s)["data"])


# ---------- state.vscdb 读写 ----------
def _conn(path: Path | None = None):
    db = str(path or STATE_DB)
    if not os.path.exists(db):
        raise SecretError(f"未找到 {db}")
    con = sqlite3.connect(db)
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return con


def is_login_key(key: str) -> bool:
    """按特征判断是否为账号登录态条目（key 名含 accessToken/refreshToken）。"""
    k = key.lower()
    return "accesstoken" in k or "refreshtoken" in k


def read_secrets(path: Path | None = None) -> dict[str, bytes]:
    """读取 DB 中该扩展全部密文条目 {key: 密文字节}。"""
    out = {}
    con = _conn(path)
    try:
        cur = con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE ?",
            (f"secret://%{EXTENSION_ID}%",))
        for k, v in cur:
            out[k] = from_value_json(v)
    finally:
        con.close()
    return out


def find_login_keys(db: Path | None = None) -> list[str]:
    """当前 DB 中所有登录态条目 key 名。"""
    con = _conn(db)
    try:
        rows = con.execute(
            "SELECT key FROM ItemTable WHERE key LIKE ?",
            (f"secret://%{EXTENSION_ID}%",)).fetchall()
        return [k for (k,) in rows if is_login_key(k)]
    finally:
        con.close()


def find_login_entries(db: Path | None = None, aes: bytes | None = None) -> dict[str, str]:
    """解密 DB 中所有登录态条目，返回 {key: 明文文本}。"""
    aes = aes or get_aes_key()
    out: dict[str, str] = {}
    for k, raw in read_secrets(db).items():
        if not is_login_key(k):
            continue
        try:
            out[k] = decrypt_v10(raw, aes)
        except Exception:
            continue
    return out


def make_secret_key(name: str) -> str:
    return f'secret://{{"extensionId":"{EXTENSION_ID}","key":"{name}"}}'


def product_storage_keys() -> list[str]:
    """当前 IDE 版本实际使用的登录态 key 名（product.json 的 storageKey）。

    国内版为 planning-genie.new.accessTokencn，旧版为 ...accessToken；
    写错 key 名 IDE 就读不到，切号会"没生效"。
    """
    out: list[str] = []
    try:
        import cb_runtime
        exe = cb_runtime.find_codebuddy_exe()
        if exe:
            pj = Path(exe).parent / "resources" / "app" / "extensions" / "genie" / "product.json"
            if pj.exists():
                data = json.loads(pj.read_text(encoding="utf-8", errors="ignore"))
                sk = ((data.get("authentication") or {}).get("attributes") or {}).get("storageKey")
                if sk:
                    out.append(make_secret_key(sk))
    except Exception:
        pass
    if not out:
        out.append(make_secret_key("planning-genie.new.accessTokencn"))
    return out


def resolve_target_keys(db: Path | None = None) -> list[str]:
    """切号时应写入的全部登录条目 key。

    同时覆盖当前版本 storageKey 与 DB 里已有的登录条目 key（旧版/新版并存时都要写），
    避免"写了 A key、IDE 读 B key"导致切号不生效。
    """
    keys: list[str] = []
    for k in product_storage_keys():
        keys.append(k)
    try:
        for k in find_login_keys(db):
            if k not in keys:
                keys.append(k)
    except Exception:
        pass
    if not keys:
        keys = [make_secret_key("planning-genie.new.accessTokencn"),
                make_secret_key("planning-genie.new.accessToken")]
    return keys


def resolve_target_key(db: Path | None = None) -> str:
    """确定写回登录态应优先使用的 key 名（兼容旧调用）。"""
    return resolve_target_keys(db)[0]


def read_current_account(path: Path | None = None, key: bytes | None = None):
    """读取当前 IDE 中该扩展的账号明文（第一组能解开的登录条目），返回 (key, 明文) 或 None。"""
    entries = find_login_entries(path, key)
    for k, plain in entries.items():
        return k, plain
    return None


def get_current_storage_id(db: Path | None = None) -> str | None:
    """当前 DB 登录条目明文里的客户端标识 id（如 Tencent-Cloud.genie-ide-cn）。"""
    got = read_current_account(db)
    if not got:
        return None
    _, plain = got
    try:
        obj = json.loads(plain)
        return obj.get("id") if isinstance(obj, dict) else None
    except Exception:
        return None


def write_secrets(entries: dict[str, bytes], db: Path | None = None,
                  drop_login_keys: bool = True) -> int:
    """把 {key: 密文} 写回 DB。默认先删除该扩展名下现有登录态条目（保留缓存/草稿类），
    再插入目标。返回写入条目数。调用方需确保 CodeBuddy 未运行。"""
    con = _conn(db)
    try:
        if drop_login_keys:
            for k in find_login_keys(db):
                con.execute("DELETE FROM ItemTable WHERE key=?", (k,))
        n = 0
        for k, raw in entries.items():
            con.execute(
                "INSERT OR REPLACE INTO ItemTable(key, value) VALUES(?, ?)",
                (k, to_value_json(raw)))
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def backup_db(db: Path | None = None) -> Path:
    """备份 DB，返回备份路径。"""
    src = Path(db) if db else STATE_DB
    if not src.exists():
        raise SecretError(f"未找到 {src}")
    dst = src.with_name(
        f"state.vscdb.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    import shutil
    # 注意 WAL 模式：最好连 -wal/-shm 一起处理；CodeBuddy 关闭状态下无 wal
    shutil.copy2(src, dst)
    return dst
