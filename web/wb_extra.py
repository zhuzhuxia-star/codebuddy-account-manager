# -*- coding: utf-8 -*-
"""WorkBuddy 补充能力：对话活跃上报、官网网页会话、积分补偿、新手礼包。

协议形状参考 workbuddy2api-panel 的协议分析（见 README-web.md 末尾致谢），
本文件为独立 Python 实现：

1) 活跃上报：POST {网关}/v2/report，body 是事件数组，一条
   chat_request_send 事件（全字段 + 上报公共指纹），userId=账号 uid 必填
   （缺失时服务端 200 但静默丢弃）。一条上报点亮成长连登、解锁领养前置。
   风控口径：每号每天 1 次，只在每日全量里跑。

2) 官网网页会话：成长中心/用量页接口只认官网 Cookie，不认 Bearer。
   获取流程（官方桌面端同款）：POST /v2/plugin/device/auth/code
   （带账号令牌+刷新令牌）换一次性登录码 → 浏览器式打开
   /console/client-login?code=..&target=.. 种下 session Cookie →
   之后只带 Cookie 发网页 XHR 形状的请求。会话按账号落盘（加密），
   有效期约 7 天；遇 401 清掉重取一次。

3) 积分补偿：POST /billing/meter/compensation-status 查状态，
   active 且未领取才 POST /billing/meter/claim-compensation。
   新手礼包：POST /billing/meter/check-gift-claimed 查，
   active 且未领取才 POST /billing/meter/claim-gift。
   两者都在官网用量页（Referer=/profile/usage）口径下。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import cb_api
import cb_log
import client_profile

REPORT_PATH = "/v2/report"
DEVICE_CODE_PATH = "/v2/plugin/device/auth/code"
CLIENT_LOGIN_PATH = "/console/client-login"
COMP_STATUS_PATH = "/billing/meter/compensation-status"
COMP_CLAIM_PATH = "/billing/meter/claim-compensation"
GIFT_CHECK_PATH = "/billing/meter/check-gift-claimed"
GIFT_CLAIM_PATH = "/billing/meter/claim-gift"
PAGE_USAGE = "/profile/usage"
PAGE_GROWTH = "/profile/growth-center"

WEB_BASE_FALLBACK = "https://www.workbuddy.cn"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/152.0.0.0 Safari/537.36")
DAEMON_UA = "WorkBuddy/5.11.3 WorkBuddy/5.11.3 CLI/2.176.0"

# 桌面端 5.11.3（国内版）上报指纹里的构建信息
_DESKTOP_COMMIT = "ecace0f7028166695994300182295329b8b95449"
_DESKTOP_RELEASE_MS = 1794501984587
_DESKTOP_VERSION = "5.11.3"
_REPORT_DELAY = 2000


# ---------- 活跃上报 ----------
def _fmt_uuid(b: bytes) -> str:
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _uuid7() -> str:
    """官方会话 id 形状：UUID v7（毫秒时间戳打头）。"""
    ms = int(time.time() * 1000)
    b = bytearray(os.urandom(16))
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return _fmt_uuid(bytes(b))


def _device_digest(uid: str, salt: str) -> bytes:
    return hashlib.sha256(f"{salt}:{uid}".encode("utf-8")).digest()[:16]


def _machine_id(uid: str) -> str:
    """桌面端 machineId：由 uid 稳定派生的大写 UUID（模拟固定设备）。"""
    return _fmt_uuid(_device_digest(uid, "machine")).upper()


def _cli_session_id(uid: str) -> str:
    """内置 CLI 的 sessionId：由 uid 稳定派生的 v4 形 UUID。"""
    b = bytearray(_device_digest(uid, "cli-session"))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return _fmt_uuid(bytes(b))


def build_activity_event(uid: str, nickname: str) -> dict:
    """一条 chat_request_send 事件：业务字段 + 上报公共指纹（业务优先）。"""
    ts = int(time.time() * 1000) - _REPORT_DELAY
    conv = _uuid7()
    req_id = _uuid7().replace("-", "")
    ev = {
        # 上报服务补的公共指纹
        "timestamp": ts, "reportDelay": _REPORT_DELAY,
        "timezone": "Asia/Shanghai",
        "userId": uid, "username": nickname, "userNickname": nickname,
        "product": "SaaS", "releaseDate": _DESKTOP_RELEASE_MS,
        "commit": _DESKTOP_COMMIT,
        "ideName": "WorkBuddy", "ideType": "WorkBuddy",
        "ideVersion": _DESKTOP_VERSION,
        "machineId": _machine_id(uid), "sessionId": _cli_session_id(uid),
        "extName": "workbuddy-desktop", "extVersion": _DESKTOP_VERSION,
        "os": "darwin", "arch": "arm64", "osVersion": "24.6.0",
        "cpuModel": "Apple M2", "cpuCores": 8, "memorySize": 16,
        # 事件业务字段（客户端 chat_request_send 全形状）
        "eventCode": "chat_request_send",
        "mode": "craft", "conversationId": conv, "requestId": req_id,
        "inputLength": 12,
        "requestModelId": "deepseek-v4-flash",
        "requestModelName": "DeepSeek V4 Flash",
        "isPlan": False, "isAutoExecuteTerminal": False, "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0, "maxSteps": 0, "temperature": 0, "maxRetries": 0,
        "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
        "codebaseId": "", "mentionContextCount": 0,
        "command": "", "expertId": "", "recommendId": "", "skillId": "",
        "skillCount": 0, "totalCount": 0, "fileUri": "",
        "presentAt": ts, "traceId": "",
        "rootRequestId": req_id, "parentConversationId": conv,
        "agentName": "default", "agentType": "conversation",
    }
    return ev


def report_activity(auth: dict, uid: str, nickname: str,
                    endpoint: str | None = None) -> dict:
    """发一条对话活跃上报。返回 {ok, error, endpoint}。

    先走配置的网关 endpoint，失败再试官网域（上报是埋点，重复一条无害）。
    """
    if not uid:
        return {"ok": False, "error": "账号缺 uid，无法上报", "endpoint": ""}
    ev = build_activity_event(uid, nickname or "")
    bases = []
    for ep in (endpoint or cb_api.resolve_endpoint(), WEB_BASE_FALLBACK):
        ep = (ep or "").rstrip("/")
        if ep and ep not in bases:
            bases.append(ep)
    err = ""
    for ep in bases:
        try:
            # 上报走后台服务框架画像 + 专用 Content-Type（;charset=UTF-8）
            resp = cb_api._request("POST", REPORT_PATH, body=[ev], auth=auth,
                                   timeout=20, endpoint=ep, profile="report",
                                   identity={"uid": uid})
            code = resp.get("code") if isinstance(resp, dict) else None
            if code in (0, None):
                return {"ok": True, "error": "", "endpoint": ep}
            err = f"code={code} {resp.get('msg') or ''}".strip()
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
    return {"ok": False, "error": err, "endpoint": ""}


# ---------- 官网网页会话 ----------
_WEB_SESSION_FILE = (Path(os.environ.get("APPDATA", ""))
                     / "CodeBuddyAccountManager" / "web_sessions.bin")


class _NeedRenew(Exception):
    """网页会话失效（401），需要清掉重取。"""


def _load_sessions() -> dict:
    try:
        import cb_secrets as cs
        raw = cs.dpapi_unprotect(_WEB_SESSION_FILE.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sessions(data: dict) -> None:
    try:
        import cb_secrets as cs
        _WEB_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
        _WEB_SESSION_FILE.write_bytes(cs.dpapi_protect(blob))
    except Exception as e:  # noqa: BLE001
        cb_log.log(f"网页会话保存失败：{e}", "web")


def _base_candidates(endpoint: str | None = None) -> list[str]:
    out = []
    for ep in (endpoint or cb_api.resolve_endpoint(), WEB_BASE_FALLBACK):
        ep = (ep or "").rstrip("/")
        if ep and ep not in out:
            out.append(ep)
    return out


def _fetch_device_code(base: str, auth: dict, uid: str) -> str:
    """POST /v2/plugin/device/auth/code 换一次性登录码（官方后台服务形状）。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*", "Accept-Language": "*", "Sec-Fetch-Mode": "cors",
        "User-Agent": "node",
        # Node fetch（undici）自动加的 Accept-Encoding
        "Accept-Encoding": ("br, gzip, deflate" if client_profile._HAS_BROTLI
                            else "gzip, deflate"),
        "Authorization": f"Bearer {auth.get('accessToken') or ''}",
        "X-User-Id": uid,
        "X-Domain": auth.get("domain") or urllib.parse.urlparse(base).hostname,
    }
    ident = client_profile.identity_for(auth) or {}
    if ident.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(ident["enterprise_id"])
        headers["X-Tenant-Id"] = str(ident["enterprise_id"])
    if auth.get("refreshToken"):
        headers["X-Refresh-Token"] = auth["refreshToken"]
    req = urllib.request.Request(base + DEVICE_CODE_PATH, data=b"",
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = client_profile.decode_body(
                resp.read(), resp.headers.get("Content-Encoding")
            ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise cb_api.ApiError(f"登录码 HTTP {e.code}: {raw[:200]}",
                              status=e.code) from e
    env = json.loads(raw) if raw else {}
    if env.get("code") != 0:
        raise cb_api.ApiError(
            f"登录码 code={env.get('code')} {env.get('msg') or ''}",
            code=env.get("code"))
    code = (env.get("data") or {}).get("deviceCode") or ""
    if not code:
        raise cb_api.ApiError("登录码为空")
    return code


def _acquire_session(base: str, auth: dict, uid: str, target: str) -> list[dict]:
    """登录码 → client-login 种 Cookie。返回 cookie 列表 [{name,value,domain,path}]。"""
    code = _fetch_device_code(base, auth, uid)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    url = (base + CLIENT_LOGIN_PATH + "?" + urllib.parse.urlencode(
        {"code": code, "target": target}))
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "User-Agent": BROWSER_UA,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-CH-UA-Platform": "\"macOS\"",
        "Accept-Encoding": "gzip, deflate, br, zstd",
    })
    try:
        with opener.open(req, timeout=25) as resp:
            resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        if e.code >= 400:
            raise cb_api.ApiError(f"client-login HTTP {e.code}",
                                  status=e.code) from e
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path} for c in jar]
    host = urllib.parse.urlparse(base).hostname
    if not any(c["name"] == "session" and c["value"]
               and (c["domain"] in (host, "." + host) or host.endswith(c["domain"].lstrip(".")))
               for c in cookies):
        raise cb_api.ApiError("client-login 未种下 session Cookie")
    return cookies


def _cookie_header(cookies: list[dict], base: str) -> str:
    host = urllib.parse.urlparse(base).hostname
    pairs = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if dom and (host == dom or host.endswith("." + dom)):
            pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs)


def _web_once(base: str, cookies: list[dict], method: str, path: str,
              referer_path: str, body, identity: dict | None = None) -> object:
    """带 Cookie 发一次网页 XHR 形状请求并解信封，返回 data。"""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Client-Platform": "web",
        "Referer": base + referer_path,
        "User-Agent": BROWSER_UA,
        "Accept-Language": ("zh-CN" if "/activity/growth" in path
                            else "zh-CN,zh;q=0.9"),
        "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-CH-UA-Platform": "\"macOS\"",
        # Chrome 口径 gzip, deflate, br, zstd；只宣告本运行时能解的编码
        "Accept-Encoding": ("gzip, deflate, br" if client_profile._HAS_BROTLI
                            else "gzip, deflate"),
        "Cookie": _cookie_header(cookies, base),
    }
    if identity and identity.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(identity["enterprise_id"])
    data = None
    if method != "GET":
        headers["Origin"] = base
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = b""
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = client_profile.decode_body(
                resp.read(), resp.headers.get("Content-Encoding")
            ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 401:
            raise _NeedRenew(f"HTTP 401: {raw[:160]}") from e
        raise cb_api.ApiError(f"HTTP {e.code}: {raw[:300]}",
                              status=e.code) from e
    env = json.loads(raw) if raw else {}
    if not isinstance(env, dict) or "code" not in env:
        raise cb_api.ApiError(f"网页接口响应异常：{raw[:160]}")
    if env.get("code") == 401:
        raise _NeedRenew(f"code=401 {env.get('msg') or ''}")
    if env.get("code") != 0:
        raise cb_api.ApiError(f"code={env.get('code')} {env.get('msg') or ''}",
                              code=env.get("code"))
    return env.get("data")


def web_json(auth: dict, uid: str, method: str, path: str, referer_path: str,
             body=None, endpoint: str | None = None):
    """发官网网页接口：优先用落盘会话，401/无会话时重新获取一次。

    返回 data；失败抛 cb_api.ApiError（message 可读）。
    """
    sessions = _load_sessions()
    saved = sessions.get(uid) or {}
    bases = _base_candidates(endpoint)
    if saved.get("base") in bases:
        bases.remove(saved["base"])
        bases.insert(0, saved["base"])
    identity = client_profile.identity_for(auth)

    def acquire_and_call() -> object:
        err = ""
        for base in bases:
            try:
                cookies = _acquire_session(base, auth, uid, referer_path)
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200]
                continue
            sessions[uid] = {"base": base, "cookies": cookies,
                             "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            _save_sessions(sessions)
            return _web_once(base, cookies, method, path, referer_path, body,
                             identity)
        raise cb_api.ApiError(f"获取网页登录态失败：{err}")

    if saved.get("cookies"):
        try:
            return _web_once(saved["base"], saved["cookies"], method, path,
                             referer_path, body, identity)
        except _NeedRenew:
            sessions.pop(uid, None)
            _save_sessions(sessions)
            return acquire_and_call()
    return acquire_and_call()


# ---------- 积分补偿 / 新手礼包 ----------
def compensation(auth: dict, uid: str, endpoint: str | None = None) -> dict:
    """查补偿状态，开放且未领就领。返回 {ok, active, claimed, credit,
    newly_claimed, end_time, error}。credit 取领取响应，缺失时用状态里的额度。"""
    out = {"ok": False, "active": False, "claimed": False, "credit": 0,
           "newly_claimed": False, "end_time": "", "error": ""}
    try:
        st = web_json(auth, uid, "POST", COMP_STATUS_PATH, PAGE_USAGE,
                      None, endpoint) or {}
        out["active"] = bool(st.get("active"))
        out["claimed"] = bool(st.get("claimed"))
        out["end_time"] = str(st.get("end_time") or "")
        credit_num = st.get("credit_num") or 0
        if out["active"] and not out["claimed"]:
            try:
                data = web_json(auth, uid, "POST", COMP_CLAIM_PATH,
                                PAGE_USAGE, None, endpoint) or {}
                credit = data.get("credit") if isinstance(data, dict) else 0
                out["credit"] = int(credit or credit_num or 0)
                out["claimed"] = True
                out["newly_claimed"] = True
            except Exception as e:  # noqa: BLE001
                # 可能刚被别处领走：回读确认
                st2 = web_json(auth, uid, "POST", COMP_STATUS_PATH,
                               PAGE_USAGE, None, endpoint) or {}
                if st2.get("claimed"):
                    out["claimed"] = True
                else:
                    raise
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    return out


def claim_gift(auth: dict, uid: str, endpoint: str | None = None) -> dict:
    """新手礼包：查状态，开放且未领就领。返回 {ok, state, credit, error}，
    state ∈ claimed_now / already / inactive / error。"""
    out = {"ok": False, "state": "error", "credit": 0, "error": ""}
    try:
        st = web_json(auth, uid, "POST", GIFT_CHECK_PATH, PAGE_USAGE,
                      None, endpoint) or {}
        if not st.get("active"):
            out.update(ok=True, state="inactive")
            return out
        if st.get("claimed"):
            out.update(ok=True, state="already")
            return out
        data = web_json(auth, uid, "POST", GIFT_CLAIM_PATH, PAGE_USAGE,
                        None, endpoint) or {}
        credit = data.get("credit") if isinstance(data, dict) else 0
        out.update(ok=True, state="claimed_now", credit=int(credit or 0))
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    return out
