# -*- coding: utf-8 -*-
"""API 网关（一期）：把账号池对外开放成 OpenAI 兼容接口。

端点（由 server.py 挂载，Bearer 密钥鉴权，不走网页登录态）：
  GET  /v1/models
  POST /v1/chat/completions   流式 SSE 透传 + 非流式聚合

协议事实来源：workbuddy2api-panel 的协议文档（docs/api.md、gateway.md、
official-client.md）所记录的官方客户端行为，本模块为 Python 独立实现，
未复制其代码：
  - 上游对话 POST https://www.workbuddy.cn/v2/chat/completions，强制 stream，
    带账号 access token 与官方客户端风格请求头（UA、X-Agent-*、会话三件套等）；
  - 模型目录 GET https://www.workbuddy.cn/v3/config（data.models[]）；
  - 模型名路由：客户端短名（如 5.3-flash）按目录后缀唯一命中改写为
    全名（glm-5.3-flash），多义/无命中不改写（上游只认目录全名，
    短名会回 400 11102 service info not found）；
  - 错误按业务码分类：402/14001 系列=额度不足，12153=会话失效，
    11101/11115/11128=请求本身的问题（不换号），429/14003/6xxx=繁忙换号。

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
from pathlib import Path

import cb_api
import cb_app
import cb_log

DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
KEY_FILE = DATA_DIR / "gateway.json"

CHAT_URL = "https://www.workbuddy.cn/v2/chat/completions"
CONFIG_URL = "https://www.workbuddy.cn/v3/config"
CHAT_TIMEOUT = 300

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


def _session_bind(key: str | None, account_id: str, conv_id: str) -> None:
    if not key:
        return
    with _sess_lock:
        _sessions[key] = {"account_id": account_id, "conv_id": conv_id,
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
def _uuid7ish() -> str:
    return uuid.uuid4().hex


def _chat_headers(auth: dict, uid: str, enterprise_id: str,
                  conv_id: str, req_id: str, msg_id: str) -> dict:
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
        "X-Client-Platform": "macos",
        "X-Client-Product": "WorkBuddy",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "1200",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "6.25.0",
        "X-Stainless-OS": "MacOS",
        "X-Stainless-Arch": "arm64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v22.21.1",
    }
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def _account_meta(rec: dict) -> tuple[str, str]:
    """(uid, enterprise_id)：uid 取记录外层，企业 id 从会话明文里挖。"""
    uid = str(rec.get("uid") or "")
    ent = ""
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
        if uid and ent:
            break
    return uid, ent


def _open_chat(auth: dict, uid: str, enterprise_id: str,
               payload: dict, conv_id: str):
    """发一发上游对话。返回 (status, resp, err_body)。
    status==200 时 resp 是活的流对象、err_body 为 b''；否则 resp 为 None。"""
    req_id = _uuid7ish()
    msg_id = _uuid7ish()
    headers = _chat_headers(auth, uid, enterprise_id, conv_id, req_id, msg_id)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CHAT_URL, data=data, headers=headers,
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
    except Exception as e:  # noqa: BLE001
        raise GatewayTransportError(str(e)) from e


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
    client 请求本身的问题（原样回客户端，不换号）/ switch 其他错误换号。"""
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


def _stream_outcome(resp, rec: dict, model: str, t0: float) -> Outcome:
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
    req = urllib.request.Request(CONFIG_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    models = ((data or {}).get("data") or {}).get("models")
    if not isinstance(models, list):
        return None
    return [m for m in models if isinstance(m, dict)] or None


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
        uid, _ent = _account_meta(rec)
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
            "max_output": m.get("maxOutputTokens"),
            "credits": m.get("credits"),
            "tags": [str(t) for t in (m.get("tags") or []) if t],
        })
    return rows or None


# ---------- 模型名路由 ----------
def _catalog_ids(store) -> list[str]:
    """当前模型目录的 id 列表：优先上游 /v3/config 目录（list_models
    内部带缓存），拉不到时 list_models 已回落到静态兜底表。"""
    try:
        data = list_models(store)
        return [str(m["id"]) for m in data.get("data") or [] if m.get("id")]
    except Exception:  # noqa: BLE001
        return list(STATIC_MODELS)


def _resolve_model(store, requested: str) -> str:
    """把客户端给的模型名解析成上游目录全名：
    与目录名完全一致 → 原样；否则按后缀匹配（目录名以 -<请求名> 结尾，
    即去掉厂商前缀后相等），恰好命中一个才改写；命中多个或零个不改写，
    原样发上游让它自己报错，不瞎猜。"""
    if not requested:
        return requested
    ids = _catalog_ids(store)
    if requested in ids:
        return requested
    suffix = "-" + requested
    hits = [mid for mid in ids if mid.endswith(suffix)]
    if len(hits) == 1:
        return hits[0]
    return requested


# ---------- 对话主流程 ----------
def chat(store, body: dict, session_header: str | None) -> Outcome:
    if not isinstance(body, dict):
        return _err_outcome(400, "bad_request", "请求体必须是 JSON 对象")
    if not isinstance(body.get("messages"), list) or not body.get("messages"):
        return _err_outcome(400, "bad_request", "messages 不能为空")
    want_stream = bool(body.get("stream"))
    model = str(body.get("model") or "auto")
    payload = _convert_body(body)
    resolved_model = _resolve_model(store, model)
    if resolved_model != model:
        cb_log.log(f"模型 {model} → {resolved_model}", "gateway")
        model = resolved_model
        payload["model"] = resolved_model
    skey = _session_key(body, session_header)
    sent = _session_lookup(skey)

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
        uid, ent = _account_meta(rec)
        conv_id = (sent or {}).get("conv_id") or str(uuid.uuid4())

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
                    _session_bind(skey, rec.get("id") or "", conv_id)
                    _log_call(rec, model, want_stream, "OK", "-",
                              time.time() - t0)
                    return Outcome("json", 200, obj)
                _session_bind(skey, rec.get("id") or "", conv_id)
                if want_stream:
                    return _stream_outcome(resp, rec, model, t0)
                out, stats = _aggregate(resp, model)
                try:
                    resp.close()
                except Exception:
                    pass
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
            last_note = f"上游 {status} {code or ''} {msg}".strip()
            _log_call(rec, model, want_stream, f"FAIL {last_note}", "-",
                      time.time() - t0)
            break

    return _err_outcome(
        503, "no_available_account",
        f"账号池暂时接不了这个请求（已试 {len(tried)} 个号）"
        + (f"：{last_note}" if last_note else ""),
        "server_error")
