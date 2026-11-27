# -*- coding: utf-8 -*-
"""API 网关（一期 + 二期）：把账号池对外开放成多协议兼容接口。

端点（由 server.py 挂载，密钥鉴权，不走网页登录态）：
  GET  /v1/models
  POST /v1/chat/completions   流式 SSE 透传 + 非流式聚合（Bearer）
  POST /v1/messages           Anthropic Messages（Claude Code；Bearer 或 x-api-key）
  POST /v1/responses          OpenAI Responses（Codex；Bearer 或 x-api-key）
  后两个端点翻译成内部 chat 流程（同一套选号/粘性/换号/续期），回复再
  翻译回各自协议，流式逐帧转换、非流式聚合。

协议事实来源：workbuddy2api-panel 的协议文档（docs/api.md、gateway.md、
official-client.md）所记录的官方客户端行为，本模块为 Python 独立实现，
未复制其代码：
  - 上游对话 POST https://www.workbuddy.cn/v2/chat/completions，强制 stream，
    带账号 access token 与官方客户端完整出站画像（UA、X-Stainless-*、
    会话/追踪头、extra_vars.harnessMetadata 步进链、设备指纹映射）；
  - 入口容灾：主入口 www.workbuddy.cn 遇 408/502/503/504 或传输错误
    （且响应体未声明 retry_policy.retry=false）改发备用入口
    wb.tencentbuddy.com 一次；主入口 60 秒内失败 3 次熔断 15 秒起
    （翻倍至 120 秒封顶），熔断期直发备用入口；
  - 模型目录 GET https://www.workbuddy.cn/v3/config（data.models[]）；
  - 模型名口径：官方目录原名原样透传，不做任何别名改写（用户定）；
  - 设备身份：每账号一份持久化设备身份（gateway_devices.json，0600），
    首次生成后永久复用，出站头里的 OS/Arch/平台取自该指纹而非宿主机；
  - 错误按业务码分类：402/14001 系列=额度不足，12153=会话失效，
    11101/11115/11128=请求本身的问题（不换号），429/14003/6xxx=繁忙换号，
    6021=上游繁忙直接回 503 不换号，11140+request illegal=账号风控禁用。

令牌纪律：只有账号记录 in_pool=True 且健康的号才接请求；401 先走现有
续期逻辑（cb_api.refresh_auth）刷一次再重试同号，续期按账号加锁单飞，
避免并发把同一条令牌链刷岔。客户端密钥明文只存 DATA_DIR/gateway.json
（权限 0600），服务端另存 sha256 哈希校验；日志只记账号尾号/模型/结果/
粗略 token 数，不记提示词正文、不记密钥。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

try:
    import brotli as _brotli
    _HAS_BROTLI = True
except Exception:  # noqa: BLE001
    _brotli = None
    _HAS_BROTLI = False

import cb_api
import cb_app
import cb_log

DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
KEY_FILE = DATA_DIR / "gateway.json"

CHAT_URL = "https://www.workbuddy.cn/v2/chat/completions"
CONFIG_URL = "https://www.workbuddy.cn/v3/config"
BACKUP_CHAT_URL = "https://wb.tencentbuddy.com/v2/chat/completions"
BACKUP_CONFIG_URL = "https://wb.tencentbuddy.com/v3/config"
CHAT_TIMEOUT = 300
FAILOVER_STATUSES = {408, 502, 503, 504}
# 与官方 CLI 内 axios 一致的 Accept-Encoding（上游实测不压缩响应，
# 本地仍实现 gzip/deflate/br 解码兜底；无 brotli 时不宣告 br）
ACCEPT_ENCODING = ("gzip, compress, deflate, br" if _HAS_BROTLI
                   else "gzip, deflate")

CLIENT_VERSION = "5.11.3"
CLI_VERSION = "2.176.0"
CHAT_UA = f"WorkBuddy/{CLIENT_VERSION} WorkBuddy/{CLIENT_VERSION} CLI/{CLI_VERSION}"

# 上游目录拿不到时的静态表（名称均已在官方目录/协议文档中确认存在）
STATIC_MODELS = [
    "glm-5.2", "glm-5.3-flash", "glm-5.3", "glm-5.0-turbo",
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "kimi-k2.8-preview", "kimi-k2.5", "minimax-m2.5",
]

QUOTA_CODES = {14001, 14012, 14013, 14014, 14018, 14019, 14022, 14023}
BUSY_CODES = {14003, 6001, 6002, 6005, 6006, 10105, 6020, 6021, 6022}
CLIENT_CODES = {11101, 11115, 11128}
MAX_ACCOUNTS_PER_REQUEST = 3
SESSION_TTL = 3600
MODELS_CACHE_TTL = 3600


# ---------- 客户端密钥 ----------
_key_lock = threading.Lock()


def _key_conf(create: bool = False) -> dict:
    try:
        data = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("key_plain"):
            return data
    except Exception:
        pass
    if not create:
        return {}
    plain = "sk-cbam-" + secrets.token_urlsafe(32)
    conf = {
        "key_plain": plain,
        "key_hash": hashlib.sha256(plain.encode("utf-8")).hexdigest(),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return conf


def ensure_key() -> str:
    """确保已生成客户端密钥，返回明文（只在服务端内部/管理页用）。"""
    with _key_lock:
        return _key_conf(create=True).get("key_plain") or ""


def key_plain() -> str:
    return ensure_key()


def key_masked() -> str:
    plain = ensure_key()
    if len(plain) <= 12:
        return plain[:3] + "••••"
    return f"{plain[:10]}••••••{plain[-4:]}"


def check_bearer(header_value: str) -> bool:
    """校验 Authorization: Bearer <key>（按 sha256 哈希常量时间比较）。"""
    if not header_value or not header_value.startswith("Bearer "):
        return False
    token = header_value[len("Bearer "):].strip()
    if not token:
        return False
    conf = _key_conf(create=True)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, conf.get("key_hash") or "")


def check_api_key(bearer_header: str, x_api_key: str) -> bool:
    """二期端点鉴权：Bearer 或 x-api-key 任一命中同一把网关密钥。
    （/v1/messages、/v1/responses 用；一期端点仍只认 Bearer。）"""
    if check_bearer(bearer_header or ""):
        return True
    token = (x_api_key or "").strip()
    if not token:
        return False
    conf = _key_conf(create=True)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, conf.get("key_hash") or "")


def error_obj(code: str, message: str, err_type: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


# ---------- 账号池 ----------
def _quota_left(rec: dict):
    q = rec.get("quota") or {}
    if not isinstance(q, dict):
        return None
    left = q.get("usage_left")
    if left is None:
        left = q.get("total_remain")
    try:
        return float(left) if left is not None else None
    except (TypeError, ValueError):
        return None


def _candidates(store, exclude_ids: set) -> list[dict]:
    """入池且健康的候选：in_pool 为真、未标记登录失效、有可用令牌、
    已知额度未耗尽。返回 [{rec, auth, left}]。"""
    out = []
    for rec in store.list_accounts():
        if rec.get("id") in exclude_ids:
            continue
        if not rec.get("in_pool"):
            continue
        if (rec.get("liveness") or {}).get("status") == "auth_dead":
            continue
        auth = cb_app.record_auth(rec)
        if not auth or not auth.get("accessToken"):
            continue
        left = _quota_left(rec)
        if left is not None and left <= 0:
            continue
        out.append({"rec": rec, "auth": auth, "left": left})
    return out


def _weighted_pick(cands: list[dict]) -> dict:
    known = [c["left"] for c in cands if c["left"] is not None]
    avg = (sum(known) / len(known)) if known else 1.0
    weights = []
    for c in cands:
        w = c["left"] if c["left"] is not None else avg
        weights.append(w if w and w > 0 else 0.0)
    if sum(weights) <= 0:
        weights = [1.0] * len(cands)
    return random.choices(cands, weights=weights, k=1)[0]


# ---------- 会话粘性 ----------
_sess_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # key -> {account_id, conv_id, exp}


def _session_key(body: dict, header_value: str | None) -> str | None:
    if header_value:
        return "h:" + header_value.strip()
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False, sort_keys=True)
            return "d:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return None


def _session_lookup(key: str | None):
    if not key:
        return None
    now = time.time()
    with _sess_lock:
        ent = _sessions.get(key)
        if ent and ent["exp"] > now:
            ent["exp"] = now + SESSION_TTL
            return ent
        _sessions.pop(key, None)
    return None


def _session_bind(key: str | None, account_id: str, conv_id: str,
                  req_id: str = "", step: int = 0, last_msg_id: str = "",
                  tool_ids: list | None = None) -> None:
    """绑定会话粘性 + 这一轮的 harness 链状态（req_id/step/上一条消息 id/
    本轮发出的 tool_call id 集合），供下一条同会话请求识别工具续跑。"""
    if not key:
        return
    with _sess_lock:
        _sessions[key] = {"account_id": account_id, "conv_id": conv_id,
                          "req_id": req_id, "step": step,
                          "last_msg_id": last_msg_id,
                          "tool_ids": list(tool_ids or []),
                          "exp": time.time() + SESSION_TTL}


# ---------- 续期（逐账号锁，单飞合并） ----------
_locks_guard = threading.Lock()
_acct_locks: dict[str, threading.Lock] = {}


def _acct_lock(account_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _acct_locks.get(account_id)
        if lk is None:
            lk = threading.Lock()
            _acct_locks[account_id] = lk
        return lk


def _refresh_account(store, rec: dict, failed_access: str) -> dict | None:
    """401 后给账号续期一次。同账号并发续期合并：若库里的 access token
    已不是刚才失败的那支，说明别人刚刷过，直接用现成的，不重复刷。
    12153 时重读一次账号库，refresh token 已被换过就用新的再试一次。
    成功返回新 auth 并写回账号库；失败返回 None。"""
    with _acct_lock(rec.get("id") or ""):
        fresh = store.get(rec.get("id") or "") or rec
        auth = cb_app.record_auth(fresh)
        if not auth:
            return None
        if failed_access and auth.get("accessToken") \
                and auth["accessToken"] != failed_access:
            return auth
        if not auth.get("refreshToken"):
            return None
        new_auth = None
        try:
            new_auth = cb_api.refresh_auth(auth)
        except Exception as e:  # noqa: BLE001
            if "12153" not in str(e):
                return None
            fresh2 = store.get(rec.get("id") or "") or fresh
            auth2 = cb_app.record_auth(fresh2)
            if not auth2 or not auth2.get("refreshToken") \
                    or auth2["refreshToken"] == auth.get("refreshToken"):
                return None
            try:
                new_auth = cb_api.refresh_auth(auth2)
                fresh = fresh2
            except Exception:  # noqa: BLE001
                return None
        if not new_auth:
            return None
        try:
            cb_app._rebuild_keys_with_auth(fresh, new_auth)
            fresh["refreshed_at"] = cb_app._now()
            store.update(fresh)
        except Exception:  # noqa: BLE001
            pass
        return new_auth


# ---------- 上游请求 ----------
def _uuid7() -> str:
    """真 UUIDv7（毫秒时间戳 + 版本/变体位），带连字符形。
    官方客户端的会话/请求/消息 id 均为 uuidv7（源码 createId→uuidv7）。"""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = ((ms << 80) | (0x7 << 76) | (secrets.randbits(12) << 64)
             | (0b10 << 62) | secrets.randbits(62))
    return str(uuid.UUID(int=value))


def _uuid7ish() -> str:
    """uuidv7 无连字符形（X-Conversation-Request-ID / Message-ID 用）。"""
    return _uuid7().replace("-", "")


# ---------- 每账号持久化设备身份 ----------
DEVICES_FILE = DATA_DIR / "gateway_devices.json"
_dev_lock = threading.Lock()
_dev_cache: dict | None = None

# 官方 macOS 客户端基线（参考网关与全部账号共用同一套设备基线；
# OS/Arch 取自这里而不是沙盒宿主机——沙盒是 Linux，对外必须呈现 mac 客户端）
_DEVICE_BASE = {
    "platform": "darwin",
    "client_platform": "macos",
    "stainless_os": "MacOS",
    "stainless_arch": "arm64",
    "os_version": "24.6.0",          # Darwin 24.6.0（macOS 15.6）
    "cpu_model": "Apple M2",
    "cpu_cores": 8,
    "memory_gb": 16,
}


def _device_identity(account_id: str) -> dict:
    """取账号的设备身份：首次为该账号生成一份（machine_id 按账号派生
    风格的大写 UUID、session_id 固定 UUID），落盘 gateway_devices.json
    （0600）后永久复用——绝不逐请求随机，随机指纹本身就是异常信号。
    machine_id/session_id 对应官方遥测指纹字段（machineId 按账号派生、
    sessionId 按账号固定）；对话请求头本身不带这两个值，但身份记录是
    唯一事实源，出站头的 OS/Arch/平台都从这里取。"""
    global _dev_cache
    account_id = account_id or "_unknown"
    with _dev_lock:
        if _dev_cache is None:
            try:
                data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
                _dev_cache = data if isinstance(data, dict) else {}
            except Exception:
                _dev_cache = {}
        ident = _dev_cache.get(account_id)
        if isinstance(ident, dict) and ident.get("machine_id"):
            return ident
        ident = dict(_DEVICE_BASE)
        ident["machine_id"] = str(uuid.uuid4()).upper()
        ident["session_id"] = str(uuid.uuid4())
        ident["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _dev_cache[account_id] = ident
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DEVICES_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(_dev_cache, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            os.replace(tmp, DEVICES_FILE)
            os.chmod(DEVICES_FILE, 0o600)
        except Exception:  # noqa: BLE001
            pass
        return ident


# 单次账号尝试的出站上下文（chat() 在每次尝试前写入，_open_chat 读取；
# 线程本地存储，并发请求互不串扰；夹具替换 _open_chat 时可忽略）
_attempt = threading.local()


def _set_attempt_ctx(ctx: dict) -> None:
    _attempt.ctx = ctx


def _attempt_ctx() -> dict:
    return getattr(_attempt, "ctx", None) or {}


def _trace_headers() -> dict:
    """官方 daemon 默认给每次模型请求注入的链路追踪头（W3C + B3）。"""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "b3": f"{trace_id}-{span_id}-1",
        "X-B3-TraceId": trace_id,
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
    }


def _chat_headers(auth: dict, uid: str, enterprise_id: str, dept_name: str,
                  identity: dict, conv_id: str, req_id: str,
                  msg_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {auth.get('accessToken') or ''}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CHAT_UA,
        "X-Requested-With": "XMLHttpRequest",
        "X-Request-ID": msg_id,
        "X-Product": "SaaS",
        "X-User-Id": uid or "",
        "X-Domain": auth.get("domain") or "www.codebuddy.cn",
        "X-Agent-Intent": "craft",
        "X-Agent-Type": "main",
        "X-Agent-Purpose": "conversation",
        "X-Agent-Runtime": "local-agent",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": CLIENT_VERSION,
        "X-Private-Data": "true",
        "X-Conversation-ID": conv_id,
        "X-Conversation-Request-ID": req_id,
        "X-Conversation-Message-ID": msg_id,
        "X-Client-Version": CLIENT_VERSION,
        "X-Client-Platform": identity.get("client_platform") or "macos",
        "X-Client-Product": "WorkBuddy",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "1200",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "6.25.0",
        "X-Stainless-OS": identity.get("stainless_os") or "MacOS",
        "X-Stainless-Arch": identity.get("stainless_arch") or "arm64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v22.21.1",
        "Accept-Encoding": ACCEPT_ENCODING,
    }
    headers.update(_trace_headers())
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if dept_name:
        headers["X-Department-Info"] = dept_name
    return headers


def _account_meta(rec: dict) -> tuple[str, str, str]:
    """(uid, enterprise_id, department_name)：uid 取记录外层，其余从
    会话明文的 account 对象里挖（官方客户端同源数据：enterpriseId、
    departmentFullName，有才带对应头）。"""
    uid = str(rec.get("uid") or "")
    ent = ""
    dept = ""
    for plain in (rec.get("keys") or {}).values():
        try:
            obj = json.loads(plain)
        except Exception:
            continue
        if isinstance(obj, dict):
            acc = obj.get("account") or {}
            if isinstance(acc, dict):
                uid = uid or str(acc.get("uid") or "")
                ent = ent or str(acc.get("enterpriseId") or "")
                dept = dept or str(acc.get("departmentFullName") or "")
        if uid and ent and dept:
            break
    return uid, ent, dept


class _DecodedResp:
    """按 Content-Encoding 解压的响应包装（gzip/deflate/br），对外保持
    readline/read/close/headers/status 接口与原始 resp 一致。
    上游当前不压缩（官方文档实测结论），这里只是与 Accept-Encoding
    宣告相匹配的兜底实现。"""

    def __init__(self, raw, encoding: str):
        self._raw = raw
        self.headers = raw.headers
        self.status = getattr(raw, "status", 200) or 200
        self._buf = bytearray()
        self._eof = False
        enc = (encoding or "").lower()
        self._dec = None
        if "br" in enc and _HAS_BROTLI:
            self._dec = _brotli.Decompressor()
        elif "gzip" in enc or "deflate" in enc:
            self._dec = zlib.decompressobj(47)  # 47 = 自动识别 zlib/gzip 头
        elif enc not in ("", "identity"):
            raise GatewayTransportError(f"unsupported content-encoding: {enc}")

    def _fill(self, want: int = 65536) -> None:
        if self._eof:
            return
        chunk = self._raw.read(65536)
        if not chunk:
            self._eof = True
            if self._dec is not None:
                try:
                    tail = self._dec.flush()
                    if tail:
                        self._buf.extend(tail)
                except Exception:
                    pass
            return
        if self._dec is None:
            self._buf.extend(chunk)
        elif _HAS_BROTLI and isinstance(self._dec, _brotli.Decompressor):
            self._buf.extend(self._dec.process(chunk))
        else:
            self._buf.extend(self._dec.decompress(chunk))

    def readline(self) -> bytes:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx + 1])
                del self._buf[:idx + 1]
                return line
            if self._eof:
                line = bytes(self._buf)
                self._buf.clear()
                return line
            self._fill()

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            while not self._eof:
                self._fill()
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < n and not self._eof:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass


def _maybe_decode(resp):
    enc = ""
    try:
        enc = resp.headers.get("Content-Encoding") or ""
    except Exception:
        enc = ""
    if not enc or enc.strip().lower() == "identity":
        return resp
    return _DecodedResp(resp, enc)


# ---------- 入口熔断（按主入口记，官方 failover.go 同款节奏） ----------
_breaker = {"fails": [], "open_until": 0.0, "level": 0}
_breaker_lock = threading.Lock()


def _primary_open() -> bool:
    with _breaker_lock:
        return time.time() < _breaker["open_until"]


def _note_primary_fail() -> None:
    now = time.time()
    with _breaker_lock:
        fails = [t for t in _breaker["fails"] if now - t < 60]
        fails.append(now)
        if len(fails) >= 3:
            dur = min(15 * (2 ** _breaker["level"]), 120)
            _breaker["open_until"] = now + dur
            _breaker["level"] += 1
            fails = []
        _breaker["fails"] = fails


def _note_primary_ok() -> None:
    with _breaker_lock:
        _breaker.update(fails=[], open_until=0.0, level=0)


def _retry_forbidden(err_body: bytes) -> bool:
    """响应体 retry_policy.retry=false 时官方不换入口。"""
    try:
        obj = json.loads((err_body or b"").decode("utf-8", "replace"))
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    for holder in (obj, obj.get("data") if isinstance(obj.get("data"), dict)
                   else {}):
        rp = (holder or {}).get("retry_policy")
        if isinstance(rp, dict) and rp.get("retry") is False:
            return True
    return False


def _post_once(url: str, headers: dict, data: bytes):
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=CHAT_TIMEOUT)
        return getattr(resp, "status", 200) or 200, resp, b""
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, None, body


def _open_chat(auth: dict, uid: str, enterprise_id: str,
               payload: dict, conv_id: str):
    """发一发上游对话。返回 (status, resp, err_body)。
    status==200 时 resp 是活的流对象、err_body 为 b''；否则 resp 为 None。
    入口容灾：主入口遇 FAILOVER_STATUSES 或传输错误（且响应体未声明
    retry_policy.retry=false）→ 同一请求（同消息 id/同追踪头）改发备用
    入口一次；主入口熔断期间直发备用入口。会话三件套 id 由 chat() 经
    线程本地上下文传入（req_id 全轮复用、msg_id 本次账号尝试内复用，
    含续期重试与换入口重发），缺失时本地生成（夹具直调兼容）。"""
    ctx = _attempt_ctx()
    req_id = ctx.get("req_id") or _uuid7ish()
    msg_id = ctx.get("msg_id") or _uuid7ish()
    identity = ctx.get("identity") or _device_identity(
        ctx.get("account_id") or uid or "")
    headers = _chat_headers(auth, uid, enterprise_id, ctx.get("dept") or "",
                            identity, conv_id, req_id, msg_id)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hosts = ([] if _primary_open() else [(CHAT_URL, True)])
    hosts.append((BACKUP_CHAT_URL, False))
    for i, (url, is_primary) in enumerate(hosts):
        is_last = i == len(hosts) - 1
        try:
            status, resp, err_body = _post_once(url, headers, data)
        except Exception as e:  # noqa: BLE001
            if is_primary and not is_last:
                _note_primary_fail()
                continue
            raise GatewayTransportError(str(e)) from e
        if status == 200:
            if is_primary:
                _note_primary_ok()
            return status, _maybe_decode(resp), b""
        if is_primary and not is_last and status in FAILOVER_STATUSES \
                and not _retry_forbidden(err_body):
            _note_primary_fail()
            continue
        return status, None, err_body
    raise GatewayTransportError("failover exhausted")  # pragma: no cover


class GatewayTransportError(RuntimeError):
    pass


def _parse_err_code(body: bytes):
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
        if isinstance(obj, dict):
            return obj.get("code"), str(obj.get("msg") or obj.get("message") or "")
    except Exception:
        pass
    return None, ""


def _classify(status: int, code, msg: str) -> str:
    """错误分类：auth 续期重试同号 / quota 额度不足换号 / busy 繁忙换号 /
    client 请求本身的问题（原样回客户端，不换号）/ restricted 账号被
    风控限制（禁用该号再换号）/ switch 其他错误换号。"""
    if code == 11140 and "request illegal" in (msg or ""):
        return "restricted"
    if status == 401 or code == 12153 or code == 401:
        return "auth"
    if status == 402 or code in QUOTA_CODES or "余额" in msg or "额度" in msg \
            or "insufficient" in msg.lower():
        return "quota"
    if code in CLIENT_CODES:
        return "client"
    if status == 429 or code in BUSY_CODES:
        return "busy"
    if status and status >= 500:
        return "switch"
    if status and 400 <= status < 500:
        return "switch"
    return "switch"


# ---------- 请求体转换（OpenAI -> 上游） ----------
_PASSTHROUGH = (
    "model", "messages", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "tools", "tool_choice", "parallel_tool_calls",
    "reasoning_effort", "reasoning", "response_format", "stop",
    "frequency_penalty", "presence_penalty", "user",
)


def _convert_body(body: dict) -> dict:
    out = {k: body[k] for k in _PASSTHROUGH if k in body}
    out["model"] = body.get("model") or "auto"
    out["stream"] = True  # 上游强制流式，非流式在本地聚合

    msgs = []
    dropped_tool_ids = set()
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        mm = {k: v for k, v in m.items()
              if k not in ("reasoning_text", "reasoning")}
        tcs = mm.get("tool_calls")
        if isinstance(tcs, list):
            kept = []
            for tc in tcs:
                name = ((tc.get("function") or {}).get("name")
                        if isinstance(tc, dict) else None)
                if name:
                    kept.append(tc)
                elif isinstance(tc, dict) and tc.get("id"):
                    dropped_tool_ids.add(tc["id"])
            if kept:
                mm["tool_calls"] = kept
            else:
                mm.pop("tool_calls", None)
        if mm.get("role") == "tool" and mm.get("tool_call_id") in dropped_tool_ids:
            continue
        if mm.get("role") == "assistant" and not mm.get("tool_calls") \
                and "content" not in mm and not mm.get("reasoning_content"):
            continue
        msgs.append(mm)
    out["messages"] = msgs

    out.setdefault("cache_retention", "long")
    effort = body.get("reasoning_effort") or \
        (body.get("reasoning") or {}).get("effort")
    if effort:
        out.setdefault("reasoning_summary", "auto")
    extra = dict(body.get("extra_vars") or {})
    extra.setdefault("harnessMetadata", json.dumps({
        "schemaVersion": 1, "harnessVersion": CLI_VERSION,
        "stepIndex": 0, "agent": {"depth": 0}}, ensure_ascii=False))
    if msgs and msgs[-1].get("role") == "user":
        extra.setdefault("initiator", "user")
    out["extra_vars"] = extra
    return out


def _apply_harness(payload: dict, step: int, prev_msg_id: str | None) -> None:
    """按官方 harness 步进链覆写 extra_vars.harnessMetadata：同一轮内
    工具续跑步进 stepIndex 并用 previous.conversationMessageId 链回
    上一条模型消息；新一轮 stepIndex=0、无 previous。键序与官方一致
    （schemaVersion, harnessVersion, stepIndex, previous?, agent）。"""
    meta: dict = {"schemaVersion": 1, "harnessVersion": CLI_VERSION,
                  "stepIndex": step}
    if prev_msg_id:
        meta["previous"] = {"conversationMessageId": prev_msg_id}
    meta["agent"] = {"depth": 0}
    extra = dict(payload.get("extra_vars") or {})
    extra["harnessMetadata"] = json.dumps(meta, ensure_ascii=False)
    payload["extra_vars"] = extra


def _turn_plan(sent: dict | None, msgs: list) -> tuple[str, int, str | None]:
    """识别本请求属于哪一轮：转换后消息尾部若是本会话上一响应发出的
    tool_call 的结果 → 同一轮续跑（复用 req_id、step+1、链 previous）；
    否则新一轮（新 req_id、step=0）。返回 (req_id, step, prev_msg_id)。"""
    if sent and sent.get("req_id"):
        tail_ids = []
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "tool" \
                    and m.get("tool_call_id"):
                tail_ids.append(m["tool_call_id"])
            else:
                break
        known = set(sent.get("tool_ids") or [])
        if tail_ids and all(t in known for t in tail_ids):
            return (sent["req_id"], int(sent.get("step") or 0) + 1,
                    sent.get("last_msg_id") or None)
    return _uuid7ish(), 0, None


# ---------- SSE 消费 ----------
def _iter_sse(resp):
    """逐行读上游 SSE，产出 (raw_line_bytes, data_obj_or_None, is_done)。"""
    while True:
        line = resp.readline()
        if not line:
            return
        obj, done = None, False
        s = line.strip()
        if s.startswith(b"data:"):
            payload = s[len(b"data:"):].strip()
            if payload == b"[DONE]":
                done = True
            elif payload:
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    obj = None
        yield line, obj, done


class _StreamStats:
    def __init__(self):
        self.content_len = 0
        self.usage = None
        self.finish = ""
        self.saw_done = False
        self.tool_ids: list[str] = []

    def feed(self, obj):
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            c = delta.get("content")
            if isinstance(c, str):
                self.content_len += len(c)
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id") \
                        and tc["id"] not in self.tool_ids:
                    self.tool_ids.append(tc["id"])
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]

    def tokens(self) -> str:
        if self.usage and self.usage.get("total_tokens"):
            return str(self.usage["total_tokens"])
        return f"~{max(1, self.content_len // 3)}"


class Outcome:
    def __init__(self, kind: str, status: int, payload=None):
        self.kind = kind          # "json" | "stream"
        self.status = status
        self.payload = payload    # dict（json）| Iterator[bytes]（stream）


def _err_outcome(status: int, code: str, message: str,
                 err_type: str = "invalid_request_error") -> Outcome:
    return Outcome("json", status, error_obj(code, message, err_type))


def _log_call(rec: dict, model: str, stream: bool, result: str,
              tokens: str, seconds: float) -> None:
    tail = cb_app._phone_tail(rec) or (rec.get("label") or "?")
    cb_log.log(f"网关调用 | 账号 {rec.get('label') or '?'}（尾号 {tail}）| "
               f"模型 {model} | {'stream' if stream else 'sync'} | {result} | "
               f"tokens {tokens} | {seconds:.1f}s", "gateway")


def _aggregate(resp, model: str) -> tuple[dict, _StreamStats]:
    """把上游 SSE 聚合成一个 chat.completion 响应。"""
    stats = _StreamStats()
    cid, created = "", 0
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_map: dict[int, dict] = {}
    finish = ""
    for _line, obj, done in _iter_sse(resp):
        if done:
            stats.saw_done = True
            continue
        if not isinstance(obj, dict):
            continue
        stats.feed(obj)
        cid = cid or str(obj.get("id") or "")
        created = created or int(obj.get("created") or 0)
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index") or 0)
                slot = tool_map.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_map:
        message["tool_calls"] = [tool_map[i] for i in sorted(tool_map)]
    out = {
        "id": cid or ("chatcmpl-" + _uuid7ish()[:24]),
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish or "stop"}],
    }
    if stats.usage:
        out["usage"] = stats.usage
    return out, stats


def _stream_outcome(resp, rec: dict, model: str, t0: float,
                    on_success=None) -> Outcome:
    stats = _StreamStats()

    def gen():
        try:
            for line, obj, done in _iter_sse(resp):
                if done:
                    stats.saw_done = True
                elif obj is not None:
                    stats.feed(obj)
                yield line if line.endswith(b"\n") else line + b"\n"
            if not stats.saw_done:
                yield b"data: [DONE]\n\n"
            if on_success is not None:
                try:
                    on_success(list(stats.tool_ids))
                except Exception:  # noqa: BLE001
                    pass
            _log_call(rec, model, True, "OK", stats.tokens(), time.time() - t0)
        except (BrokenPipeError, ConnectionResetError):
            _log_call(rec, model, True, "CLIENT_ABORT", stats.tokens(),
                      time.time() - t0)
        except Exception as e:  # noqa: BLE001
            _log_call(rec, model, True, f"STREAM_ERR {e}", stats.tokens(),
                      time.time() - t0)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return Outcome("stream", 200, gen())


# ---------- 模型列表 ----------
_models_cache: dict = {"at": 0.0, "data": None}
_models_lock = threading.Lock()


def _fetch_catalog_raw(auth: dict, uid: str) -> list[dict] | None:
    """GET /v3/config 取官方模型目录原文（data.models[]，不裁剪）。
    失败返回 None。"""
    headers = {
        "Authorization": f"Bearer {auth.get('accessToken') or ''}",
        "Accept": "application/json",
        "User-Agent": CHAT_UA,
        "X-Domain": auth.get("domain") or "www.codebuddy.cn",
        "X-User-Id": uid or "",
        "Connection": "close",
    }
    last_exc = None
    for url in (CONFIG_URL, BACKUP_CONFIG_URL):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in FAILOVER_STATUSES:
                last_exc = e
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            continue
        models = ((data or {}).get("data") or {}).get("models")
        if not isinstance(models, list):
            return None
        return [m for m in models if isinstance(m, dict)] or None
    if last_exc is not None:
        raise last_exc
    return None


def _trim_catalog(models: list[dict]) -> list[dict] | None:
    """把官方目录原文裁成 /v1/models 的 OpenAI 形状（口径与最初版本一致）。"""
    out = []
    for m in models:
        if not m.get("id"):
            continue
        # 补全类模型（只有 supportsExtra、没有输入上限）不是对话模型，跳过
        if m.get("supportsExtra") and not (m.get("maxInputTokens")
                                           or m.get("contextWindow")):
            continue
        entry = {"id": str(m["id"]), "object": "model", "created": 0,
                 "owned_by": "workbuddy"}
        ctx = m.get("contextWindow") or m.get("maxInputTokens")
        if ctx:
            entry["context_length"] = ctx
        if m.get("maxOutputTokens"):
            entry["max_output_tokens"] = m["maxOutputTokens"]
        if m.get("credits") is not None:
            entry["credits"] = m["credits"]
        out.append(entry)
    return out or None


def _fetch_catalog(auth: dict, uid: str) -> list[dict] | None:
    """GET /v3/config 取模型目录（裁剪版，供 /v1/models）。失败返回 None。
    成功时把目录原文一并存进共享缓存（_models_cache["raw"]），供设置页
    参考区同源展示，保证两处看到的是同一次获取的同一份目录。"""
    models = _fetch_catalog_raw(auth, uid)
    if models is None:
        return None
    with _models_lock:
        _models_cache.update(raw=models, raw_at=time.time())
    return _trim_catalog(models)


def list_models(store) -> dict:
    now = time.time()
    with _models_lock:
        if _models_cache["data"] is not None \
                and now - _models_cache["at"] < MODELS_CACHE_TTL:
            return _models_cache["data"]
    data = None
    cands = _candidates(store, set())
    if not cands:
        for rec in store.list_accounts():
            if (rec.get("liveness") or {}).get("status") == "auth_dead":
                continue
            auth = cb_app.record_auth(rec)
            if auth and auth.get("accessToken"):
                cands = [{"rec": rec, "auth": auth, "left": None}]
                break
    if cands:
        rec, auth = cands[0]["rec"], cands[0]["auth"]
        uid, _ent, _dept = _account_meta(rec)
        try:
            entries = _fetch_catalog(auth, uid)
        except Exception:  # noqa: BLE001
            entries = None
        if entries:
            data = {"object": "list", "data": entries}
    if data is None:
        data = {"object": "list", "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "workbuddy"}
            for mid in STATIC_MODELS]}
    with _models_lock:
        _models_cache.update(at=now, data=data)
    return data


def _display_context(m: dict) -> str | None:
    """目录原文里的上下文大小，原样呈现：contextWindow 档位字典给
    「默认（可选 档位…）」，只有 maxInputTokens 就给该值，都没有返回 None。"""
    cw = m.get("contextWindow")
    if isinstance(cw, dict) and cw.get("defaultLength"):
        lengths = cw.get("supportedLengths")
        if isinstance(lengths, list) and lengths:
            opts = " / ".join(str(x) for x in lengths)
            return f"{cw['defaultLength']}（可选 {opts}）"
        return str(cw["defaultLength"])
    if m.get("maxInputTokens"):
        return str(m["maxInputTokens"])
    return None


def catalog_display(store) -> list[dict] | None:
    """设置页参考区的模型行：与 /v1/models 同源（同一次 /v3/config 获取、
    同一缓存 _models_cache）。拉不到实时目录返回 None——展示区不拿
    静态兜底表冒充实时数据。每行 {id, context, max_output, credits, tags}，
    数值均为官方目录原值，不换算、不取整。"""
    def _fresh_raw():
        with _models_lock:
            raw = _models_cache.get("raw")
            at = _models_cache.get("raw_at", 0)
        if raw and time.time() - at < MODELS_CACHE_TTL:
            return raw
        return None

    raw = _fresh_raw()
    if raw is None:
        list_models(store)  # 触发同源获取；成功时 _fetch_catalog 回填 raw
        raw = _fresh_raw()
    if not raw:
        return None
    rows = []
    for m in raw:
        if not m.get("id"):
            continue
        rows.append({
            "id": str(m["id"]),
            "context": _display_context(m),
            "context_num": _context_num(m),
            "max_output": m.get("maxOutputTokens"),
            "credits": m.get("credits"),
            "credits_num": _credits_num(m.get("credits")),
            "tags": [str(t) for t in (m.get("tags") or []) if t],
            **_reasoning_fields(m),
        })
    return rows or None


_EFFORT_RANK = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}


def _context_num(m: dict):
    """上下文的数值排序键：contextWindow.defaultLength 或 maxInputTokens。"""
    cw = m.get("contextWindow")
    if isinstance(cw, dict) and cw.get("defaultLength"):
        return cw["defaultLength"]
    return m.get("maxInputTokens")


def _credits_num(credits):
    """积分倍率排序键：'x0.08' → 0.08；目录未给返回 None。"""
    if credits is None:
        return None
    s = str(credits).lstrip("xX")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _reasoning_fields(m: dict) -> dict:
    """思考档位（官方目录 reasoning 对象原样取值，不换算）：
    reasoning_supported = supportedEfforts 原列表（无该字段为 None）；
    reasoning_default = defaultEffort 或固定 effort（都无为 None）；
    reasoning_can_disable = canDisableThinking（reasoning 内优先，
    其次模型顶层同名字段；都无为 None）；
    reasoning_rank = 默认档位的排序权重（low<medium<high<xhigh<max）。"""
    r = m.get("reasoning")
    r = r if isinstance(r, dict) else {}
    supported = r.get("supportedEfforts")
    if not (isinstance(supported, list) and supported):
        supported = None
    default = r.get("defaultEffort") or r.get("effort")
    can_disable = r.get("canDisableThinking")
    if can_disable is None:
        can_disable = m.get("canDisableThinking")
    return {
        "reasoning_supported": [str(x) for x in supported] if supported else None,
        "reasoning_default": str(default) if default else None,
        "reasoning_can_disable": can_disable
        if isinstance(can_disable, bool) else None,
        "reasoning_rank": _EFFORT_RANK.get(str(default).lower())
        if default else None,
    }


# ---------- 对话主流程 ----------
def chat(store, body: dict, session_header: str | None) -> Outcome:
    if not isinstance(body, dict):
        return _err_outcome(400, "bad_request", "请求体必须是 JSON 对象")
    if not isinstance(body.get("messages"), list) or not body.get("messages"):
        return _err_outcome(400, "bad_request", "messages 不能为空")
    want_stream = bool(body.get("stream"))
    model = str(body.get("model") or "auto")
    payload = _convert_body(body)
    # 模型名口径：官方目录原名原样透传，不做任何别名改写（用户定）。
    payload["model"] = model
    skey = _session_key(body, session_header)
    sent = _session_lookup(skey)
    req_id, step, prev_msg = _turn_plan(sent, payload.get("messages") or [])
    _apply_harness(payload, step, prev_msg)

    tried: set = set()
    last_note = ""
    t0 = time.time()
    for _attempt_no in range(MAX_ACCOUNTS_PER_REQUEST):
        cands = _candidates(store, tried)
        if not cands:
            break
        pick = None
        if sent:
            for c in cands:
                if c["rec"].get("id") == sent["account_id"]:
                    pick = c
                    break
        if pick is None:
            pick = _weighted_pick(cands)
        rec, auth = pick["rec"], pick["auth"]
        tried.add(rec.get("id") or "")
        uid, ent, dept = _account_meta(rec)
        conv_id = (sent or {}).get("conv_id") or _uuid7()
        # 本次账号尝试的消息 id：续期重试与换入口重发都复用它（官方
        # 失败重试语义）；换到另一个账号才换新 id。req_id 全轮复用。
        msg_id = _uuid7ish()
        _set_attempt_ctx({
            "req_id": req_id, "msg_id": msg_id, "dept": dept,
            "identity": _device_identity(rec.get("id") or ""),
            "account_id": rec.get("id") or "",
        })

        refreshed = False
        while True:
            try:
                status, resp, err_body = _open_chat(auth, uid, ent, payload,
                                                    conv_id)
            except GatewayTransportError as e:
                last_note = f"网络错误：{e}"
                _log_call(rec, model, want_stream, f"FAIL {last_note}", "-",
                          time.time() - t0)
                break
            if status == 200:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in ctype:
                    raw = resp.read()
                    try:
                        resp.close()
                    except Exception:
                        pass
                    code, msg = _parse_err_code(raw)
                    if code not in (0, None):
                        cls = _classify(200, code, msg)
                        if cls == "client":
                            _log_call(rec, model, want_stream,
                                      f"UPSTREAM {code} {msg}", "-",
                                      time.time() - t0)
                            return _err_outcome(400, str(code),
                                                msg or "上游拒绝该请求")
                        last_note = f"上游业务错误 {code} {msg}"
                        _log_call(rec, model, want_stream, f"FAIL {last_note}",
                                  "-", time.time() - t0)
                        break
                    # 非流式的完整 JSON 响应（少见），原样回给客户端
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        obj = {"raw": raw.decode("utf-8", "replace")}
                    passthrough_tool_ids = []
                    try:
                        for ch in obj.get("choices") or []:
                            for tc in ((ch.get("message") or {})
                                       .get("tool_calls") or []):
                                if isinstance(tc, dict) and tc.get("id"):
                                    passthrough_tool_ids.append(tc["id"])
                    except Exception:  # noqa: BLE001
                        pass
                    _session_bind(skey, rec.get("id") or "", conv_id,
                                  req_id, step, msg_id, passthrough_tool_ids)
                    _log_call(rec, model, want_stream, "OK", "-",
                              time.time() - t0)
                    return Outcome("json", 200, obj)
                if want_stream:
                    acct_id = rec.get("id") or ""
                    return _stream_outcome(
                        resp, rec, model, t0,
                        on_success=lambda tids: _session_bind(
                            skey, acct_id, conv_id, req_id, step, msg_id,
                            tids))
                out, stats = _aggregate(resp, model)
                try:
                    resp.close()
                except Exception:
                    pass
                _session_bind(skey, rec.get("id") or "", conv_id,
                              req_id, step, msg_id, stats.tool_ids)
                _log_call(rec, model, False, "OK", stats.tokens(),
                          time.time() - t0)
                return Outcome("json", 200, out)

            code, msg = _parse_err_code(err_body)
            cls = _classify(status, code, msg)
            if cls == "auth" and not refreshed:
                refreshed = True
                new_auth = _refresh_account(store, rec,
                                            auth.get("accessToken") or "")
                if new_auth:
                    auth = new_auth
                    continue
                cb_app._mark_auth_dead(store, rec.get("id") or "",
                                       "网关续期失败（12153 类）")
                last_note = "登录失效（续期失败）"
                _log_call(rec, model, want_stream, f"FAIL {last_note}", "-",
                          time.time() - t0)
                break
            if cls == "client":
                _log_call(rec, model, want_stream,
                          f"UPSTREAM {status} {code} {msg}", "-",
                          time.time() - t0)
                return _err_outcome(status or 400, str(code or "upstream_error"),
                                    msg or "上游拒绝该请求")
            if cls == "restricted":
                # 11140 + request illegal：账号被上游风控限制，按官方
                # 网关行为禁用该号（网页端会显示失效原因），再换号。
                cb_app._mark_auth_dead(store, rec.get("id") or "",
                                       "上游风控限制（11140 request illegal）")
                last_note = "账号被风控限制（11140 request illegal）"
                _log_call(rec, model, want_stream, f"FAIL {last_note}", "-",
                          time.time() - t0)
                break
            if code == 6021:
                # 官方口径：6021 回 503 upstream_busy，不换号不扣分。
                _log_call(rec, model, want_stream,
                          f"UPSTREAM {status} 6021 {msg}", "-",
                          time.time() - t0)
                return _err_outcome(503, "upstream_busy",
                                    msg or "上游繁忙，请稍后重试",
                                    "server_error")
            last_note = f"上游 {status} {code or ''} {msg}".strip()
            _log_call(rec, model, want_stream, f"FAIL {last_note}", "-",
                      time.time() - t0)
            break

    return _err_outcome(
        503, "no_available_account",
        f"账号池暂时接不了这个请求（已试 {len(tried)} 个号）"
        + (f"：{last_note}" if last_note else ""),
        "server_error")


# ---------- 二期：Anthropic Messages / OpenAI Responses 翻译层 ----------
# 两个端点都翻译成内部 OpenAI chat 形状走 chat()（同一套选号/粘性/
# 换号/续期/出站画像），回复再翻译回去；流式逐帧转换、非流式聚合。
# 丢弃口径与官方网关一致：Anthropic 的 cache_control / metadata.user_id /
# context_management / thinking 块、Responses 的 store / include /
# prompt_cache_key（仅内部用作会话粘性提示，不发上游）/ 内置 web_search
# 工具一律不发上游；Claude thinking / output_config.effort 与 Codex
# reasoning.effort 映射为 reasoning_effort。

_ANTH_ERR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
                   403: "permission_error", 404: "not_found_error",
                   429: "rate_limit_error", 503: "overloaded_error",
                   529: "overloaded_error"}
_STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use",
             "length": "max_tokens", "content_filter": "refusal"}


def _anthropic_error_outcome(status: int, message: str) -> Outcome:
    return Outcome("json", status, {"type": "error", "error": {
        "type": _ANTH_ERR_TYPES.get(status, "api_error"),
        "message": message}})


def _sse_chunks(byte_iter):
    """把上游 SSE 字节流（chat() 的 stream Outcome）解析成 chunk dict 序列。"""
    buf = b""
    for piece in byte_iter:
        if not piece:
            continue
        buf += piece
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = line.strip()
            if s.startswith(b"data:"):
                p = s[len(b"data:"):].strip()
                if p and p != b"[DONE]":
                    try:
                        yield json.loads(p.decode("utf-8", "replace"))
                    except Exception:  # noqa: BLE001
                        pass
    s = buf.strip()
    if s.startswith(b"data:"):
        p = s[len(b"data:"):].strip()
        if p and p != b"[DONE]":
            try:
                yield json.loads(p.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass


# ----- Anthropic -> 内部 chat -----
def _anthropic_to_chat(body: dict):
    """返回 (chat_body, session_hint)。session_hint 仅内部用于粘性
    （metadata.user_id 的哈希），不发上游。"""
    msgs: list[dict] = []
    system = body.get("system")
    if system:
        if isinstance(system, str):
            text = system
        else:
            text = "\n".join(
                b.get("text") or "" for b in system
                if isinstance(b, dict) and b.get("type") == "text")
        if text:
            msgs.append({"role": "system", "content": text})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_msgs: list[dict] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append({"type": "text", "text": b.get("text") or ""})
            elif bt == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    url = (f"data:{src.get('media_type') or 'image/png'}"
                           f";base64,{src.get('data') or ''}")
                    parts.append({"type": "image_url",
                                  "image_url": {"url": url}})
                elif src.get("type") == "url" and src.get("url"):
                    parts.append({"type": "image_url",
                                  "image_url": {"url": src["url"]}})
            elif bt == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or "", "type": "function",
                    "function": {"name": b.get("name") or "",
                                 "arguments": json.dumps(
                                     b.get("input") or {},
                                     ensure_ascii=False)}})
            elif bt == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    text = "\n".join(
                        x.get("text") or "" for x in c
                        if isinstance(x, dict) and x.get("type") == "text")
                else:
                    text = ""
                tool_msgs.append({"role": "tool",
                                  "tool_call_id": b.get("tool_use_id") or "",
                                  "content": text})
            # thinking / redacted_thinking 等块按协议口径丢弃
        msgs.extend(tool_msgs)
        if parts or tool_calls:
            msg: dict = {"role": role}
            if parts:
                if all(p["type"] == "text" for p in parts):
                    msg["content"] = "".join(p["text"] for p in parts)
                else:
                    msg["content"] = parts
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
    chat: dict = {"model": body.get("model") or "auto", "messages": msgs,
                  "stream": bool(body.get("stream"))}
    tools = []
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("name") \
                and isinstance(t.get("input_schema"), dict):
            tools.append({"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description") or "",
                "parameters": t["input_schema"]}})
    if tools:
        chat["tools"] = tools
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        tt = tc.get("type")
        if tt == "auto":
            chat["tool_choice"] = "auto"
        elif tt == "any":
            chat["tool_choice"] = "required"
        elif tt == "none":
            chat["tool_choice"] = "none"
        elif tt == "tool" and tc.get("name"):
            chat["tool_choice"] = {"type": "function",
                                   "function": {"name": tc["name"]}}
    elif isinstance(tc, str):
        chat["tool_choice"] = tc
    for k in ("max_tokens", "temperature", "top_p"):
        if body.get(k) is not None:
            chat[k] = body[k]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"]
    effort = None
    oc = body.get("output_config") or {}
    if isinstance(oc, dict) and oc.get("effort"):
        effort = oc["effort"]
    th = body.get("thinking") or {}
    if isinstance(th, dict):
        if th.get("type") == "enabled" and not effort:
            budget = th.get("budget_tokens") or 0
            effort = ("low" if budget < 4096 else
                      "medium" if budget < 16384 else "high")
        elif th.get("type") == "adaptive" and not effort:
            effort = "medium"
    if effort:
        chat["reasoning_effort"] = effort
    hint = None
    md = body.get("metadata") or {}
    if isinstance(md, dict) and md.get("user_id"):
        hint = "anthropic:" + hashlib.sha256(
            str(md["user_id"]).encode("utf-8")).hexdigest()[:32]
    return chat, hint


# ----- 内部 chat -> Anthropic -----
def _chat_to_anthropic(resp: dict, model: str) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    blocks: list[dict] = []
    if msg.get("reasoning_content"):
        blocks.append({"type": "thinking",
                       "thinking": msg["reasoning_content"]})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:  # noqa: BLE001
            inp = {"_arguments": fn.get("arguments") or ""}
        if not isinstance(inp, dict):
            inp = {"value": inp}
        blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                       "name": fn.get("name") or "", "input": inp})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = resp.get("usage") or {}
    rid = str(resp.get("id") or "")
    mid = "msg_" + (rid[len("chatcmpl-"):] if rid.startswith("chatcmpl-")
                    else (rid or _uuid7ish()[:24]))
    return {
        "id": mid, "type": "message", "role": "assistant",
        "model": resp.get("model") or model,
        "content": blocks,
        "stop_reason": _STOP_MAP.get(ch.get("finish_reason") or "stop",
                                     "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                  "output_tokens": usage.get("completion_tokens") or 0},
    }


def _anthropic_stream(byte_iter, model: str):
    """OpenAI SSE chunk 流 -> Anthropic 事件流（逐帧转换）。"""
    def ev(t: str, obj: dict) -> bytes:
        return (f"event: {t}\n"
                f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode()

    mid = "msg_" + _uuid7ish()[:24]
    started = False
    open_idx = None
    open_type = None
    next_idx = 0
    tools: dict[int, int] = {}
    finish = ""
    out_tokens = 0

    def start_msg():
        return ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    for obj in _sse_chunks(byte_iter):
        if not isinstance(obj, dict):
            continue
        if not started:
            started = True
            yield start_msg()
        if isinstance(obj.get("usage"), dict) \
                and obj["usage"].get("completion_tokens"):
            out_tokens = obj["usage"]["completion_tokens"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            for kind, field, dtype in (
                    ("thinking", "reasoning_content", "thinking_delta"),
                    ("text", "content", "text_delta")):
                val = delta.get(field)
                if not (isinstance(val, str) and val):
                    continue
                if open_type != kind:
                    if open_idx is not None:
                        yield ev("content_block_stop", {
                            "type": "content_block_stop", "index": open_idx})
                    open_idx = next_idx
                    next_idx += 1
                    open_type = kind
                    block = ({"type": "thinking", "thinking": ""}
                             if kind == "thinking" else
                             {"type": "text", "text": ""})
                    yield ev("content_block_start", {
                        "type": "content_block_start", "index": open_idx,
                        "content_block": block})
                yield ev("content_block_delta", {
                    "type": "content_block_delta", "index": open_idx,
                    "delta": {"type": dtype, kind: val}})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                ti = int(tc.get("index") or 0)
                if ti not in tools:
                    if open_idx is not None:
                        yield ev("content_block_stop", {
                            "type": "content_block_stop", "index": open_idx})
                    open_idx = next_idx
                    next_idx += 1
                    open_type = "tool_use"
                    tools[ti] = open_idx
                    fn = tc.get("function") or {}
                    yield ev("content_block_start", {
                        "type": "content_block_start", "index": open_idx,
                        "content_block": {
                            "type": "tool_use", "id": tc.get("id") or "",
                            "name": fn.get("name") or "", "input": {}}})
                args = (tc.get("function") or {}).get("arguments")
                if args:
                    yield ev("content_block_delta", {
                        "type": "content_block_delta", "index": tools[ti],
                        "delta": {"type": "input_json_delta",
                                  "partial_json": args}})
    if not started:
        yield start_msg()
    if open_idx is not None:
        yield ev("content_block_stop", {"type": "content_block_stop",
                                        "index": open_idx})
    yield ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": _STOP_MAP.get(finish or "stop", "end_turn"),
                  "stop_sequence": None},
        "usage": {"output_tokens": out_tokens}})
    yield ev("message_stop", {"type": "message_stop"})


# ----- Responses -> 内部 chat -----
def _responses_to_chat(body: dict):
    """返回 (chat_body, custom_tool_names, session_hint)。"""
    msgs: list[dict] = []
    custom: set = set()
    instr = body.get("instructions")
    if isinstance(instr, str) and instr:
        msgs.append({"role": "system", "content": instr})
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        pending: list[dict] = []

        def flush():
            if pending:
                msgs.append({"role": "assistant", "content": "",
                             "tool_calls": list(pending)})
                pending.clear()

        for item in inp:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t in ("function_call", "custom_tool_call"):
                if t == "custom_tool_call":
                    args = json.dumps({"input": item.get("input") or ""},
                                      ensure_ascii=False)
                else:
                    args = item.get("arguments") or "{}"
                pending.append({
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {"name": item.get("name") or "",
                                 "arguments": args}})
                continue
            flush()
            if t == "message":
                role = item.get("role") or "user"
                if role == "developer":
                    role = "user"
                content = item.get("content")
                if isinstance(content, str):
                    msgs.append({"role": role, "content": content})
                    continue
                parts = []
                for c in content or []:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("input_text", "output_text", "text"):
                        parts.append({"type": "text",
                                      "text": c.get("text") or ""})
                    elif ct == "input_image" and c.get("image_url"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": c["image_url"]}})
                if parts:
                    if all(p["type"] == "text" for p in parts):
                        msgs.append({"role": role, "content": "".join(
                            p["text"] for p in parts)})
                    else:
                        msgs.append({"role": role, "content": parts})
            elif t == "function_call_output":
                out = item.get("output")
                msgs.append({"role": "tool",
                             "tool_call_id": item.get("call_id") or "",
                             "content": out if isinstance(out, str) else
                             json.dumps(out or "", ensure_ascii=False)})
            # reasoning 等条目按协议口径丢弃
        flush()
    chat: dict = {"model": body.get("model") or "auto", "messages": msgs,
                  "stream": bool(body.get("stream"))}
    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tt = t.get("type")
        if tt == "function" and t.get("name"):
            fn = {"name": t["name"],
                  "description": t.get("description") or "",
                  "parameters": t.get("parameters") or
                  {"type": "object", "properties": {}}}
            if t.get("strict") is not None:
                fn["strict"] = t["strict"]
            tools.append({"type": "function", "function": fn})
        elif tt == "custom" and t.get("name"):
            # Codex 自定义工具（如 apply_patch）-> 函数工具，参数是
            # {"input": 字符串}，回复时再还原成 custom_tool_call。
            custom.add(t["name"])
            tools.append({"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description") or "",
                "parameters": {"type": "object",
                               "properties": {"input": {"type": "string"}},
                               "required": ["input"]}}})
        # web_search 等内置工具按协议口径丢弃
    if tools:
        chat["tools"] = tools
    tc = body.get("tool_choice")
    if isinstance(tc, str):
        chat["tool_choice"] = tc
    elif isinstance(tc, dict) and tc.get("type") in ("function", "custom") \
            and tc.get("name"):
        chat["tool_choice"] = {"type": "function",
                               "function": {"name": tc["name"]}}
    r = body.get("reasoning") or {}
    if isinstance(r, dict) and r.get("effort"):
        chat["reasoning_effort"] = r["effort"]
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls"):
        if body.get(k) is not None:
            chat[k] = body[k]
    hint = None
    if body.get("prompt_cache_key"):
        hint = "responses:" + hashlib.sha256(
            str(body["prompt_cache_key"]).encode("utf-8")).hexdigest()[:32]
    return chat, custom, hint


# ----- 内部 chat -> Responses -----
def _unwrap_custom_args(args_str) -> str:
    try:
        obj = json.loads(args_str or "")
        if isinstance(obj, dict) and isinstance(obj.get("input"), str):
            return obj["input"]
    except Exception:  # noqa: BLE001
        pass
    return args_str or ""


def _chat_to_response(resp: dict, model: str, custom) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    output: list[dict] = []
    if msg.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + _uuid7ish()[:24],
                       "summary": [{"type": "summary_text",
                                    "text": msg["reasoning_content"]}],
                       "status": "completed"})
    if msg.get("content"):
        output.append({"type": "message", "id": "msg_" + _uuid7ish()[:24],
                       "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text",
                                    "text": msg["content"],
                                    "annotations": []}]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name in custom:
            output.append({"type": "custom_tool_call",
                           "id": "ctc_" + _uuid7ish()[:24],
                           "call_id": tc.get("id") or "", "name": name,
                           "input": _unwrap_custom_args(fn.get("arguments")),
                           "status": "completed"})
        else:
            output.append({"type": "function_call",
                           "id": "fc_" + _uuid7ish()[:24],
                           "call_id": tc.get("id") or "", "name": name,
                           "arguments": fn.get("arguments") or "",
                           "status": "completed"})
    usage = resp.get("usage") or {}
    return {
        "id": "resp_" + _uuid7ish()[:24], "object": "response",
        "created_at": int(resp.get("created") or time.time()),
        "status": "completed", "model": resp.get("model") or model,
        "output": output, "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                  "output_tokens": usage.get("completion_tokens") or 0,
                  "total_tokens": usage.get("total_tokens") or
                  ((usage.get("prompt_tokens") or 0) +
                   (usage.get("completion_tokens") or 0))},
    }


def _responses_stream(byte_iter, model: str, custom):
    """OpenAI SSE chunk 流 -> Responses response.* 事件流（逐帧转换）。
    自定义工具的参数 JSON 在内部流里是 {"input": ...} 分片，无法逐片
    还原成原始 input 文本，故整段缓冲、完成时一次性发出。"""
    def ev(t: str, obj: dict) -> bytes:
        o = dict(obj)
        o["type"] = t
        return (f"event: {t}\n"
                f"data: {json.dumps(o, ensure_ascii=False)}\n\n").encode()

    resp_id = "resp_" + _uuid7ish()[:24]
    created = int(time.time())
    yield ev("response.created", {"response": {
        "id": resp_id, "object": "response", "created_at": created,
        "status": "in_progress", "model": model, "output": []}})
    next_idx = 0
    text_item = None      # {id, output_index, buf}
    reason_item = None
    calls: dict[int, dict] = {}   # oai index -> {id, call_id, name, buf, custom, output_index, started}
    text_buf: list[str] = []
    reason_buf: list[str] = []
    usage = None
    finish = ""

    def close_text():
        nonlocal text_item
        if text_item is None:
            return
        full = "".join(text_buf)
        item = {"type": "message", "id": text_item["id"], "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": full,
                             "annotations": []}]}
        yield ev("response.output_text.done", {
            "item_id": text_item["id"],
            "output_index": text_item["output_index"],
            "content_index": 0, "text": full})
        yield ev("response.content_part.done", {
            "item_id": text_item["id"],
            "output_index": text_item["output_index"],
            "content_index": 0,
            "part": {"type": "output_text", "text": full, "annotations": []}})
        yield ev("response.output_item.done", {
            "output_index": text_item["output_index"], "item": item})
        text_item = None

    def close_reason():
        nonlocal reason_item
        if reason_item is None:
            return
        full = "".join(reason_buf)
        yield ev("response.reasoning_summary_text.done", {
            "item_id": reason_item["id"],
            "output_index": reason_item["output_index"],
            "summary_index": 0, "text": full})
        yield ev("response.reasoning_summary_part.done", {
            "item_id": reason_item["id"],
            "output_index": reason_item["output_index"],
            "summary_index": 0,
            "part": {"type": "summary_text", "text": full}})
        yield ev("response.output_item.done", {
            "output_index": reason_item["output_index"],
            "item": {"type": "reasoning", "id": reason_item["id"],
                     "summary": [{"type": "summary_text", "text": full}],
                     "status": "completed"}})
        reason_item = None

    def close_call(st):
        args = "".join(st["buf"])
        if st["custom"]:
            inp = _unwrap_custom_args(args)
            item = {"type": "custom_tool_call", "id": st["id"],
                    "call_id": st["call_id"], "name": st["name"],
                    "input": inp, "status": "completed"}
            yield ev("response.output_item.added", {
                "output_index": st["output_index"], "item": {
                    "type": "custom_tool_call", "id": st["id"],
                    "call_id": st["call_id"], "name": st["name"],
                    "input": "", "status": "in_progress"}})
            yield ev("response.custom_tool_call_input.delta", {
                "item_id": st["id"], "output_index": st["output_index"],
                "delta": inp})
            yield ev("response.custom_tool_call_input.done", {
                "item_id": st["id"], "output_index": st["output_index"],
                "input": inp})
            yield ev("response.output_item.done", {
                "output_index": st["output_index"], "item": item})
        else:
            yield ev("response.function_call_arguments.done", {
                "item_id": st["id"], "output_index": st["output_index"],
                "arguments": args})
            yield ev("response.output_item.done", {
                "output_index": st["output_index"], "item": {
                    "type": "function_call", "id": st["id"],
                    "call_id": st["call_id"], "name": st["name"],
                    "arguments": args, "status": "completed"}})

    for obj in _sse_chunks(byte_iter):
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            rc = delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                if reason_item is None:
                    rid = "rs_" + _uuid7ish()[:24]
                    reason_item = {"id": rid, "output_index": next_idx}
                    next_idx += 1
                    yield ev("response.output_item.added", {
                        "output_index": reason_item["output_index"],
                        "item": {"type": "reasoning", "id": rid,
                                 "summary": [], "status": "in_progress"}})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rid,
                        "output_index": reason_item["output_index"],
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""}})
                reason_buf.append(rc)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": reason_item["id"],
                    "output_index": reason_item["output_index"],
                    "summary_index": 0, "delta": rc})
            c = delta.get("content")
            if isinstance(c, str) and c:
                if text_item is None:
                    mid = "msg_" + _uuid7ish()[:24]
                    text_item = {"id": mid, "output_index": next_idx}
                    next_idx += 1
                    yield ev("response.output_item.added", {
                        "output_index": text_item["output_index"],
                        "item": {"type": "message", "id": mid,
                                 "role": "assistant", "status": "in_progress",
                                 "content": []}})
                    yield ev("response.content_part.added", {
                        "item_id": mid,
                        "output_index": text_item["output_index"],
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "",
                                 "annotations": []}})
                text_buf.append(c)
                yield ev("response.output_text.delta", {
                    "item_id": text_item["id"],
                    "output_index": text_item["output_index"],
                    "content_index": 0, "delta": c})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                ti = int(tc.get("index") or 0)
                fn = tc.get("function") or {}
                st = calls.get(ti)
                if st is None:
                    name = fn.get("name") or ""
                    st = {"id": "fc_" + _uuid7ish()[:24],
                          "call_id": tc.get("id") or "", "name": name,
                          "buf": [], "custom": name in custom,
                          "output_index": next_idx, "started": False}
                    next_idx += 1
                    calls[ti] = st
                    if not st["custom"]:
                        st["started"] = True
                        yield ev("response.output_item.added", {
                            "output_index": st["output_index"], "item": {
                                "type": "function_call", "id": st["id"],
                                "call_id": st["call_id"], "name": name,
                                "arguments": "", "status": "in_progress"}})
                if fn.get("name") and not st["name"]:
                    st["name"] = fn["name"]
                    st["custom"] = st["name"] in custom
                if tc.get("id") and not st["call_id"]:
                    st["call_id"] = tc["id"]
                args = fn.get("arguments")
                if args:
                    st["buf"].append(args)
                    if not st["custom"]:
                        yield ev("response.function_call_arguments.delta", {
                            "item_id": st["id"],
                            "output_index": st["output_index"],
                            "delta": args})
    yield from close_reason()
    yield from close_text()
    tool_calls_out = []
    for ti in sorted(calls):
        st = calls[ti]
        yield from close_call(st)
        tool_calls_out.append({
            "id": st["call_id"], "type": "function",
            "function": {"name": st["name"],
                         "arguments": "".join(st["buf"])}})
    synth = {"id": resp_id, "created": created, "model": model,
             "choices": [{"message": {
                 "role": "assistant", "content": "".join(text_buf),
                 **({"reasoning_content": "".join(reason_buf)}
                    if reason_buf else {}),
                 **({"tool_calls": tool_calls_out} if tool_calls_out
                    else {})},
                 "finish_reason": finish or "stop"}],
             "usage": usage or {}}
    final = _chat_to_response(synth, model, custom)
    final["id"] = resp_id
    final["created_at"] = created
    yield ev("response.completed", {"response": final})


# ----- 端点入口 -----
def _wrap_outcome(out: Outcome, proto: str, model: str,
                  custom=frozenset()) -> Outcome:
    if out.kind == "stream":
        if proto == "anthropic":
            return Outcome("stream", 200, _anthropic_stream(out.payload,
                                                            model))
        return Outcome("stream", 200, _responses_stream(out.payload, model,
                                                        custom))
    payload = out.payload if isinstance(out.payload, dict) else {}
    if "error" in payload:
        if proto == "anthropic":
            err = payload.get("error") or {}
            return _anthropic_error_outcome(
                out.status, err.get("message") or "上游错误")
        return out
    if proto == "anthropic":
        return Outcome("json", 200, _chat_to_anthropic(payload, model))
    return Outcome("json", 200, _chat_to_response(payload, model, custom))


def messages(store, body, session_header: str | None) -> Outcome:
    """POST /v1/messages（Anthropic Messages，Claude Code）。"""
    if not isinstance(body, dict):
        return _anthropic_error_outcome(400, "请求体必须是 JSON 对象")
    chat_body, hint = _anthropic_to_chat(body)
    if not chat_body.get("messages"):
        return _anthropic_error_outcome(400, "messages 不能为空")
    model = str(body.get("model") or "auto")
    out = chat(store, chat_body, session_header or hint)
    return _wrap_outcome(out, "anthropic", model)


def responses(store, body, session_header: str | None) -> Outcome:
    """POST /v1/responses（OpenAI Responses，Codex）。"""
    if not isinstance(body, dict):
        return _err_outcome(400, "bad_request", "请求体必须是 JSON 对象")
    chat_body, custom, hint = _responses_to_chat(body)
    if not chat_body.get("messages"):
        return _err_outcome(400, "bad_request", "input 不能为空")
    model = str(body.get("model") or "auto")
    out = chat(store, chat_body, session_header or hint)
    return _wrap_outcome(out, "responses", model, custom)
