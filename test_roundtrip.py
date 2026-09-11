# -*- coding: utf-8 -*-
"""写回链路验证：在 state.vscdb 的副本上 读->解->重加密->写->再解，确认一致。
不会改动真实 DB。CodeBuddy 运行时副本可能读到旧快照，仅用于验证算法。"""
import shutil
import tempfile
from pathlib import Path

import cb_secrets as cs

tmp = Path(tempfile.mkdtemp(prefix="cb_vsc_test_"))
dst = tmp / "state.vscdb"
shutil.copy2(cs.STATE_DB, dst)

aes = cs.get_aes_key()
print("AES key ok, len =", len(aes))
print("using DB:", cs.STATE_DB)

secrets = cs.read_secrets(dst)
login = {k: v for k, v in secrets.items() if cs.is_login_key(k)}
print("total entries:", len(secrets), "| login entries:", len(login))
assert login, "无登录条目"
k0, raw0 = next(iter(login.items()))
print("login key:", k0)

plain0 = cs.decrypt_v10(raw0, aes)
print("decrypt ok, plain len =", len(plain0))

new_raw = cs.encrypt_v10(plain0, aes)
n = cs.write_secrets({k0: new_raw}, db=dst, drop_login_keys=True)
print("written:", n)

secrets2 = cs.read_secrets(dst)
# 删除登录条目又插入新的，条目总数不变；缓存/草稿类条目应原样保留
assert len(secrets2) == len(secrets), (len(secrets2), len(secrets))
assert k0 in secrets2
plain1 = cs.decrypt_v10(secrets2[k0], aes)
assert plain0 == plain1, "ROUNDTRIP MISMATCH"
# 非登录条目未被误删
cache_keys = {k for k in secrets if not cs.is_login_key(k)}
assert cache_keys <= set(secrets2), "缓存条目被误删"
print("ROUNDTRIP OK: 登录条目写回一致，缓存/草稿条目保留")
