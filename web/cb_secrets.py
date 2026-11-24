# -*- coding: utf-8 -*-
"""网页版 cb_secrets 替身（Linux）：DPAPI -> 本地密钥 AES-256-GCM。

桌面版用 Windows DPAPI 保护 accounts.bin；服务器上没有 DPAPI，这里用一把
存放在数据目录的随机密钥（store.key，权限 0600）做等价的落盘静态加密，
接口与桌面版 cb_secrets 的 dpapi_protect / dpapi_unprotect 完全一致，
account_store / cb_cloud 无需改动即可复用。

IDE 本地功能（state.vscdb 读写、切号）在网页版不可用，相关函数调用即抛
SecretError —— cb_app 里这些路径都有 try/except 或仅由桌面功能触发。
"""
from __future__ import annotations

import os
from pathlib import Path


class SecretError(RuntimeError):
    pass


_DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
_KEY_FILE = _DATA_DIR / "store.key"
_MAGIC = b"CBW1"


def _key() -> bytes:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    k = os.urandom(32)
    _KEY_FILE.write_bytes(k)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    return k


def dpapi_protect(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return _MAGIC + nonce + AESGCM(_key()).encrypt(nonce, data, None)


def dpapi_unprotect(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(_MAGIC):
        raise SecretError("账号库不是网页版加密格式（无法解密桌面版 DPAPI 数据）")
    nonce, ct = blob[4:16], blob[16:]
    return AESGCM(_key()).decrypt(nonce, ct, None)


def get_current_storage_id():
    """网页版没有本机 IDE，返回 None（cb_app 会回退到默认客户端标识）。"""
    return None


def _unavailable(*_a, **_k):
    raise SecretError("该功能是桌面版专属（读写本机 CodeBuddy IDE），网页版不可用")


find_login_entries = _unavailable
read_current_account = _unavailable
get_aes_key = _unavailable
encrypt_v10 = _unavailable
decrypt_v10 = _unavailable
backup_db = _unavailable
write_secrets = _unavailable
resolve_target_keys = _unavailable
