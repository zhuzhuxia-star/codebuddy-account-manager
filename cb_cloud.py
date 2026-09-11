# -*- coding: utf-8 -*-
"""云端保险库：把账号登录态**加密后**存到 md.dcio.eu.org（telegra-lite），实现跨电脑同步。

接口（已实测确认）：
  - 发布：POST /api/publish   {"title","author","content_html","content_text"}
  - 更新：POST /api/update    {"title","author","content_html","content_text","slug"}
          两者都返回 {"errcode":0,"data":{"slug","alias","path","url","alias_url",...}}
  - 读取：GET /{slug}         页面 HTML，正文文本在
          `<meta property="og:description" content="...">`（本站会放全文）

安全约定（不可绕过）：
  - 上传前必须用 PBKDF2-HMAC-SHA256(口令, salt, 200k) 派生 AES-256-GCM 密钥加密，
    密文 `salt(16)|nonce(12)|ct|tag(16)` 再 base64，只把 base64 放进文章正文。
    即使文章被任何人访问，没有口令也无法还原账号。
  - 标题固定（→ 别名固定），新电脑可直接用别名 URL 找到同一篇；本地另存 slug 用于更新。
  - 推送后立即回读校验，解密结果与原文不一致会明确报错，不会静默成功。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import zlib
from pathlib import Path

import cb_log
import cb_secrets as cs

BASE = "https://md.dcio.eu.org"
VAULT_TITLE = "CBAC-VAULT-v1"          # 固定标题 → 固定别名 /CBAC-VAULT-v1
STORE_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
CONF_FILE = STORE_DIR / "cloud.json"
PBKDF2_ITER = 200_000
SALT_BYTES = 16
UA = "curl/8.0.1"

# 只上传"换台电脑继续用"所需的字段（不含额度/签到缓存等易变数据）
_KEEP_FIELDS = ("id", "uid", "email", "label", "keys", "source", "created_at")


class CloudError(RuntimeError):
    pass


# ---------- 本地配置（slug / 口令） ----------
def load_conf() -> dict:
    try:
        if CONF_FILE.exists():
            data = json.loads(CONF_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        return {}
    except Exception:
        return {}


def save_conf(conf: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_passphrase(nbytes: int = 24) -> str:
    """生成随机同步口令（URL-safe，约 32 字符，方便抄写/粘贴）。"""
    import secrets

    return secrets.token_urlsafe(nbytes)


def ensure_passphrase() -> str:
    """本机已有口令则返回，否则生成一个并存下来（供无人值守/一键上传用）。"""
    p = saved_passphrase()
    if p:
        return p
    p = generate_passphrase()
    save_passphrase(p)
    return p


def save_passphrase(passphrase: str) -> None:
    """把口令用 DPAPI 加密后存本机（仅当前 Windows 用户可解），便于无人值守同步。"""
    conf = load_conf()
    conf["pass_enc"] = base64.b64encode(cs.dpapi_protect(passphrase.encode("utf-8"))).decode()
    save_conf(conf)


def saved_passphrase() -> str:
    conf = load_conf()
    enc = conf.get("pass_enc")
    if not enc:
        return ""
    try:
        return cs.dpapi_unprotect(base64.b64decode(enc)).decode("utf-8")
    except Exception:
        return ""


def vault_url() -> str:
    conf = load_conf()
    slug = conf.get("slug") or VAULT_TITLE
    return f"{BASE}/{slug}"


# ---------- 加解密 ----------
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITER)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_payload(records: list[dict], passphrase: str) -> str:
    """账号记录 -> base64 密文（可直接放进文章正文）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not passphrase:
        raise CloudError("未设置同步口令，无法加密上传")
    slim = [{k: r.get(k) for k in _KEEP_FIELDS if k in r} for r in records]
    raw = json.dumps({"v": 2, "at": int(time.time()),
                      "accounts": slim}, ensure_ascii=False).encode("utf-8")
    blob = zlib.compress(raw, 6)   # 会话明文里有大量重复 JSON，压缩率很高
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(12)
    sealed = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, blob, None)
    return base64.b64encode(salt + nonce + sealed).decode("ascii")


def decrypt_payload(token: str, passphrase: str) -> list[dict]:
    """文章正文里的 base64 密文 -> 账号记录列表。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not passphrase:
        raise CloudError("未提供同步口令，无法解密")
    raw = re.sub(r"[^A-Za-z0-9+/=]", "", token or "")
    try:
        data = base64.b64decode(raw)
    except Exception as e:
        raise CloudError(f"云端内容不是有效的 base64：{e}") from e
    if len(data) < SALT_BYTES + 12 + 16:
        raise CloudError("云端内容太短，可能不是本工具写入的保险库")
    salt, nonce, sealed = data[:SALT_BYTES], data[SALT_BYTES:SALT_BYTES + 12], data[SALT_BYTES + 12:]
    try:
        plain = AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, sealed, None)
    except Exception as e:
        raise CloudError("解密失败：同步口令不正确，或云端内容已损坏") from e
    try:
        blob = zlib.decompress(plain)      # v2 起内容先压缩
    except Exception:
        blob = plain                       # 兼容未压缩的历史内容
    try:
        obj = json.loads(blob.decode("utf-8"))
        accounts = obj.get("accounts") or []
    except Exception as e:
        raise CloudError(f"云端内容解析失败：{e}") from e
    if not isinstance(accounts, list) or not accounts:
        raise CloudError("云端保险库里没有账号数据")
    return accounts


# ---------- HTTP（网关会拦默认 UA，用 curl.exe；失败回退 urllib） ----------
def _curl(args: list[str], *, body: str | None = None, timeout: int = 40) -> str:
    """调用 curl.exe（网关会拦默认 UA）。

    注意：body 一律用 `--data-binary @-` 从 stdin 传入 —— Windows 下 `@C:\\path`
    会被 curl 当成协议前缀而报 "error encountered when reading a file"。
    """
    r = subprocess.run(["curl.exe", "-s", "--max-time", str(timeout), *args],
                       input=body, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 and not r.stdout:
        raise CloudError(f"curl 执行失败：{(r.stderr or '').strip()[:200]}")
    return r.stdout or ""


def _post_json(api: str, payload: dict) -> dict:
    out = _curl(["-X", "POST", "-H", "Content-Type: application/json; charset=utf-8",
                 "--data-binary", "@-", BASE + api],
                body=json.dumps(payload, ensure_ascii=False))
    try:
        return json.loads(out)
    except Exception as e:
        raise CloudError(f"接口返回无法解析（{api}）：{out[:200]}") from e


def _get_html(path: str) -> str:
    p = path if path.startswith("/") else "/" + path
    return _curl(["-H", f"User-Agent: {UA}", BASE + p], timeout=30)


def _extract_article_token(html: str) -> str:
    """从文章页面取回密文。

    注意：`og:description` 会被**截断到约 160 字符**，不能用于长内容；正文里的密文
    是完整保存在页面 HTML 中的，因此取页面里最长的一段 base64。
    """
    best = ""
    for m in re.finditer(r"[A-Za-z0-9+/=]{120,}", html):
        if len(m.group(0)) > len(best):
            best = m.group(0)
    if best:
        return best
    # 兜底：短内容（<120 字符）时用 og:description
    m = re.search(r'<meta\s+property="og:description"\s+content="(.*?)"\s*/?>', html, re.S)
    if not m:
        raise CloudError("云端文章不存在，或该页面不是本工具写入的保险库（未找到密文）")
    text = m.group(1)
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#39;", "'"), ("&#x27;", "'"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    if len(text.strip()) < 40:
        raise CloudError("云端文章正文过短，不是本工具写入的保险库")
    return text


# ---------- 业务 ----------
def push(records: list[dict], passphrase: str, verify: bool = True) -> tuple[bool, str]:
    """把账号记录加密上传（首次 publish，之后 update 同一篇）。"""
    if not records:
        return False, "账号库为空，没什么可上传"
    try:
        token = encrypt_payload(records, passphrase)
    except CloudError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"加密失败：{e}"

    conf = load_conf()
    slug = conf.get("slug") or ""
    payload = {
        "title": VAULT_TITLE,
        "author": "cbac",
        "content_html": f"<p>{token}</p>",
        "content_text": token,
    }
    try:
        if slug:
            payload["slug"] = slug
            resp = _post_json("/api/update", payload)
        else:
            resp = _post_json("/api/publish", payload)
    except CloudError as e:
        return False, str(e)

    if resp.get("errcode") != 0:
        return False, f"上传失败：{resp.get('errtext') or resp}"
    data = resp.get("data") or {}
    new_slug = data.get("slug") or slug
    if new_slug:
        conf["slug"] = new_slug
        conf["alias"] = data.get("alias") or VAULT_TITLE
        conf["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        conf["accounts"] = len(records)
        save_conf(conf)
    cb_log.log(f"云端上传 | {len(records)} 个账号 | slug={new_slug} | "
               f"{data.get('url') or ''}", "cloud")

    if verify:
        ok, got = pull(passphrase, ref=new_slug)
        if not ok:
            return False, f"已上传但回读校验失败：{got}"
        if len(got) != len(records):
            return False, f"已上传但回读数量不一致（{len(got)} vs {len(records)}）"
        cb_log.log("云端回读校验通过", "cloud")
    return True, f"已加密上传 {len(records)} 个账号\n{data.get('url') or vault_url()}"


def pull(passphrase: str, ref: str | None = None) -> tuple[bool, list[dict] | str]:
    """从云端取回账号记录。ref 可传 slug / 完整 URL；默认用本地记录的 slug。"""
    conf = load_conf()
    target = (ref or conf.get("slug") or VAULT_TITLE).strip()
    m = re.search(r"md\.dcio\.eu\.org/(.+)$", target)
    if m:
        target = m.group(1)
    target = target.lstrip("/")
    try:
        html = _get_html(target)
        token = _extract_article_token(html)
        body = token.strip()
        if len(body) < 120 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", body):
            raise CloudError("云端文章正文不是本工具的保险库格式（请确认文章 URL/slug）")
        accounts = decrypt_payload(token, passphrase)
    except CloudError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"读取云端失败：{e}"
    cb_log.log(f"云端下载 | {len(accounts)} 个账号 | ref={target}", "cloud")
    return True, accounts


def merge_accounts(local: list[dict], remote: list[dict]) -> tuple[list[dict], int, int]:
    """按 uid 合并（云端优先覆盖同名 uid 的登录态），返回 (合并后, 新增数, 更新数)。"""
    out = list(local)
    index = {a.get("uid"): i for i, a in enumerate(out) if a.get("uid")}
    added = updated = 0
    for r in remote:
        uid = r.get("uid")
        if uid and uid in index:
            i = index[uid]
            merged = dict(out[i])
            for k in _KEEP_FIELDS:      # 云端登录态为准，本地展示字段保留
                if r.get(k) not in (None, "", {}):
                    merged[k] = r[k]
            out[i] = merged
            updated += 1
        else:
            if not r.get("id"):
                r["id"] = uid or os.urandom(8).hex()
            out.append(r)
            added += 1
    return out, added, updated
