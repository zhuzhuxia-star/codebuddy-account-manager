# -*- coding: utf-8 -*-
"""官方客户端出站画像（管理器侧）：请求头构造、链路追踪、设备基线、身份绑定。

口径依据 workbuddy2api-panel 的协议文档 docs/official-client.md（只学协议
事实，独立实现），与二期网关（gateway.py）同源：

- 后台服务框架类（登录/刷新/上报/成长等插件 API）：daemon UA
  `WorkBuddy/<版本> WorkBuddy/<版本> CLI/<cli>` + 框架公共头
  （Accept: application/json, text/plain, */*、X-Requested-With、
  X-Request-ID、X-Product: SaaS、X-Domain、X-Client-* 身份头组、
  Accept-Encoding 按 axios 口径）；
- AuthService 类（积分/签到）：UA `axios/1.16.1`，头只有 buildHeaders
  那组（Accept/Content-Type、X-Request-ID、Authorization、X-User-Id、
  企业头、X-Domain、X-Client-*），积分查询另带 Accept-Language: zh；
- 登录/刷新另带 traceService 根 span 链路头；从登录界面发起的登录再带
  子 span 的 traceparent（同一类请求多次轮询共用一个子 span）；
- 设备基线与 gateway.py 共用同一事实源：直接读 gateway_devices.json
  里已落盘的身份（OS/Arch/平台都从那里取），文件不存在时用与
  gateway._DEVICE_BASE 逐字相同的常量兜底，不另建一套身份；
- 文档没写清的字段一律不发（不编造）。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
import zlib
from pathlib import Path

try:
    import brotli as _brotli
    _HAS_BROTLI = True
except Exception:  # noqa: BLE001
    _brotli = None
    _HAS_BROTLI = False

CLIENT_VERSION = "5.11.3"
CLI_VERSION = "2.176.0"
APP_NAME = "WorkBuddy"
DAEMON_UA = f"{APP_NAME}/{CLIENT_VERSION} {APP_NAME}/{CLIENT_VERSION} CLI/{CLI_VERSION}"
AXIOS_UA = "axios/1.16.1"
FRAMEWORK_ACCEPT = "application/json, text/plain, */*"

DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
DEVICES_FILE = DATA_DIR / "gateway_devices.json"

# 与 gateway._DEVICE_BASE 逐字一致（gateway_devices.json 缺失时的兜底；
# 正常情况下基线从已落盘的设备身份里读，与网关同源）
_DEVICE_BASE_FALLBACK = {
    "platform": "darwin",
    "client_platform": "macos",
    "stainless_os": "MacOS",
    "stainless_arch": "arm64",
    "os_version": "24.6.0",
    "cpu_model": "Apple M2",
    "cpu_cores": 8,
    "memory_gb": 16,
}

_baseline_cache: dict | None = None


def device_baseline() -> dict:
    """设备基线：优先读 gateway_devices.json 里任一已落盘身份的基线字段
    （与网关出站头同源），读不到用同值常量兜底。"""
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache
    base = dict(_DEVICE_BASE_FALLBACK)
    try:
        data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for ident in data.values():
                if isinstance(ident, dict) and ident.get("machine_id"):
                    for k in base:
                        if ident.get(k) is not None:
                            base[k] = ident[k]
                    break
    except Exception:  # noqa: BLE001
        pass
    _baseline_cache = base
    return base


def accept_encoding() -> str:
    """axios 的 Accept-Encoding（gzip, compress, deflate, br）。
    运行时没有 brotli 解码能力时不宣告 br（与 gateway.py 的能力口径
    一致），避免上游真回 br 时解不开。"""
    return "gzip, compress, deflate, br" if _HAS_BROTLI else "gzip, deflate"


def decode_body(raw: bytes, encoding: str | None) -> bytes:
    """按 Content-Encoding 解压响应体（gzip/deflate/br）。
    解不了的编码或解压失败时原样返回（与改动前行为一致，不吞数据）。"""
    if not raw or not encoding:
        return raw
    data = raw
    tokens = [t.strip().lower() for t in str(encoding).split(",") if t.strip()]
    try:
        for tok in reversed(tokens):
            if tok in ("gzip", "deflate"):
                data = zlib.decompressobj(47).decompress(data)
            elif tok == "br" and _HAS_BROTLI:
                data = _brotli.decompress(data)
            elif tok in ("identity", "compress"):
                continue
            else:
                return raw
    except Exception:  # noqa: BLE001
        return raw
    return data


def new_request_id() -> str:
    """官方 X-Request-ID 形状：crypto.randomUUID() 去连字符（32 位 hex）。"""
    return uuid.uuid4().hex


# ---------- 链路追踪（登录/刷新用根 span；登录观测用子 span） ----------
def root_trace_headers() -> dict:
    """traceService.startTrace 根 span 的链路头（登录、刷新令牌用）：
    b3 以 `-1-` 结尾（父 span 为空），X-B3-ParentSpanId 为空值。"""
    trace_id = uuid.uuid4().hex
    span_id = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace_id,
        "b3": f"{trace_id}-{span_id}-1-",
        "X-B3-TraceId": trace_id,
        "X-B3-ParentSpanId": "",
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
    }


class LoginTrace:
    """一次登录流程的链路上下文（按「从登录界面发起」口径）：
    loginSessionId 一个（其去连字符形即 trace id），根 span 一个，
    每类请求（state/token/account）一个子 span，轮询共用。"""

    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.trace_id = self.session_id.replace("-", "")
        self.owner_id = str(uuid.uuid4())
        self._root_span = secrets.token_hex(8)
        self._child_spans: dict[str, str] = {}

    def headers(self, kind: str) -> dict:
        span = self._child_spans.get(kind)
        if span is None:
            span = secrets.token_hex(8)
            self._child_spans[kind] = span
        return {
            "X-Trace-ID": self.trace_id,
            "b3": f"{self.trace_id}-{self._root_span}-1-",
            "X-B3-TraceId": self.trace_id,
            "X-B3-ParentSpanId": "",
            "X-B3-SpanId": self._root_span,
            "X-B3-Sampled": "1",
            "traceparent": f"00-{self.trace_id}-{span}-01",
        }


# ---------- 账号身份（供 X-User-Id / 企业头 / 部门头取值） ----------
def extract_identity(rec: dict) -> dict:
    """从账号记录提取 (uid, enterprise_id, department)：uid 取记录外层，
    其余从会话明文的 account 对象里挖（与 gateway._account_meta 同源
    数据：enterpriseId、departmentFullName，有才带对应头）。"""
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
    return {"uid": uid, "enterprise_id": ent, "department": dept}


_bindings: dict[str, dict] = {}
_BINDINGS_MAX = 512


def _auth_key(auth: dict | None) -> str:
    tok = (auth or {}).get("refreshToken") or (auth or {}).get("accessToken") or ""
    return hashlib.sha256(str(tok).encode("utf-8")).hexdigest() if tok else ""


def bind_auth(auth: dict | None, identity: dict | None) -> None:
    """把一份 auth（令牌）与账号身份绑定，供 _request 按令牌找回身份。
    只存令牌的 sha256，不存令牌原文。"""
    key = _auth_key(auth)
    if not key or not identity:
        return
    if len(_bindings) >= _BINDINGS_MAX:
        _bindings.clear()
    _bindings[key] = dict(identity)


def identity_for(auth: dict | None) -> dict | None:
    key = _auth_key(auth)
    return _bindings.get(key) if key else None


def propagate_binding(old_auth: dict | None, new_auth: dict | None) -> None:
    """续期后把旧令牌的身份绑定传给新令牌（令牌轮换不断链）。"""
    ident = identity_for(old_auth)
    if ident:
        bind_auth(new_auth, ident)


# ---------- 请求头构造 ----------
def _client_identity_headers() -> dict:
    base = device_baseline()
    return {
        "X-Client-Version": CLIENT_VERSION,
        "X-Client-Platform": base.get("client_platform") or "macos",
        "X-Client-Product": APP_NAME,
    }


def framework_headers(auth: dict | None, domain: str, identity: dict | None = None,
                      *, has_body: bool = True,
                      content_type: str = "application/json") -> dict:
    """后台服务请求框架（restOperations）公共头。"""
    headers = {
        "Accept": FRAMEWORK_ACCEPT,
        "X-Requested-With": "XMLHttpRequest",
        "X-Request-ID": new_request_id(),
        "X-Product": "SaaS",
        "User-Agent": DAEMON_UA,
        "Accept-Encoding": accept_encoding(),
        "X-Domain": domain,
    }
    if has_body:
        headers["Content-Type"] = content_type
    headers.update(_client_identity_headers())
    if auth and auth.get("accessToken"):
        headers["Authorization"] = f"Bearer {auth['accessToken']}"
    identity = identity or {}
    if identity.get("uid"):
        headers["X-User-Id"] = str(identity["uid"])
    if identity.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(identity["enterprise_id"])
        headers["X-Tenant-Id"] = str(identity["enterprise_id"])
    if identity.get("department"):
        headers["X-Department-Info"] = str(identity["department"])
    return headers


def authservice_headers(auth: dict | None, domain: str,
                        identity: dict | None = None, *,
                        accept_language: bool = False) -> dict:
    """AuthService（积分/签到）那组头：只有 buildHeaders 给的这些，
    不带 X-Requested-With / X-Product / X-Department-Info / X-IDE-* / 链路头。
    X-User-Id 没有 uid 时按官方发空串。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": AXIOS_UA,
        "Accept-Encoding": accept_encoding(),
        "X-Request-ID": new_request_id(),
    }
    if auth and auth.get("accessToken"):
        headers["Authorization"] = f"Bearer {auth['accessToken']}"
    identity = identity or {}
    headers["X-User-Id"] = str(identity.get("uid") or "")
    if identity.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(identity["enterprise_id"])
        headers["X-Tenant-Id"] = str(identity["enterprise_id"])
    if domain:
        headers["X-Domain"] = domain
    headers.update(_client_identity_headers())
    if accept_language:
        headers["Accept-Language"] = "zh"
    return headers
