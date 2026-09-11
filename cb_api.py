# -*- coding: utf-8 -*-
"""CodeBuddy 服务端接口客户端（登录 / 账号 / 额度 / 续期）。

接口与 CodeBuddy IDE 内置插件完全一致（取自内置 genie 扩展的
ExternalLinkAuthenticationProvider）：

  1) POST /v2{prefix}/auth/state?platform=ide   -> {state, authUrl}
  2) 浏览器打开 authUrl 完成登录
  3) GET  /v2{prefix}/auth/token?state=xxx      轮询取 token（未就绪 code=11217）
  4) GET  /v2{prefix}/login/account?state=xxx   取账号信息（未就绪 code=12151）
  5) GET  /v2{prefix}/accounts                  账号列表
  6) POST /v2{prefix}/auth/token/refresh        续期（X-Refresh-Token）
  7) POST /v2/billing/meter/get-payment-type    套餐类型
  8) POST /v2/billing/meter/get-user-resource   额度明细

endpoint 默认取 IDE 安装目录下 genie/product.json 的 endpoint，
可用环境变量 CB_API_ENDPOINT 覆盖。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import cb_log

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
PREFIX_PATH = "/plugin"
PLATFORM = "ide"
USER_AGENT = "curl/8.0.1"

# 轮询重试码（与 IDE 一致：等用户完成浏览器登录）
RETRY_FETCH_TOKEN = 11217
RETRY_FETCH_ACCOUNT = 12151
RETRY_CODES = (RETRY_FETCH_TOKEN, RETRY_FETCH_ACCOUNT)

H_NO_AUTH = "X-No-Authorization"
H_NO_USER = "X-No-User-Id"
H_NO_ENTERPRISE = "X-No-Enterprise-Id"
H_NO_DEPARTMENT = "X-No-Department-Info"
H_DOMAIN = "X-Domain"
H_REFRESH_TOKEN = "X-Refresh-Token"
H_REFRESH_SOURCE = "X-Auth-Refresh-Source"

PAYMENT_LABELS = {
    "free": "免费版",
    "pro": "专业版",
    "ultimate": "旗舰版",
    "exclusive": "专属版",
    "enterprise": "企业版",
}

# ---- 额度资源枚举（与 IDE 内置 BackendProvider.getCurrentPlan 一致）----
COMMODITY_FREE = "TCACA_code_001_PqouKr6QWV"        # 每日免费额度包
COMMODITY_PRO_MON = "TCACA_code_002_AkiJS3ZHF5"     # 专业版月包
COMMODITY_PRO_MON_PLUS = "TCACA_code_005_maRGyrHhw1"
COMMODITY_GIFT = "TCACA_code_006_DbXS0lrypC"        # 赠送包
COMMODITY_ACTIVITY = "TCACA_code_007_nzdH5h4Nl0"    # 运营活动包
COMMODITY_PRO_YEAR = "TCACA_code_003_FAnt7lcmRT"    # 专业版年包
COMMODITY_FREE_MON = "TCACA_code_008_cfWoLwvjU4"    # 免费月体验包
COMMODITY_EXTRA = "TCACA_code_009_0XmEQc2xOf"       # 加量包
PRO_CODES = {COMMODITY_PRO_MON, COMMODITY_PRO_MON_PLUS, COMMODITY_PRO_YEAR}
TRIAL_CODES = {COMMODITY_GIFT, COMMODITY_FREE_MON}
_ACCOUNT_STATUS_VALID = 0
_ACCOUNT_STATUS_USED_UP = 3
_PRODUCT_CODE = "p_tcaca"


class ApiError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class CanceledError(RuntimeError):
    pass


# ---------- endpoint ----------
def resolve_endpoint() -> str:
    env = os.environ.get("CB_API_ENDPOINT", "").strip()
    if env:
        return env.rstrip("/")
    try:
        import cb_runtime
        exe = cb_runtime.find_codebuddy_exe()
        if exe:
            pj = Path(exe).parent / "resources" / "app" / "extensions" / "genie" / "product.json"
            if pj.exists():
                data = json.loads(pj.read_text(encoding="utf-8", errors="ignore"))
                ep = data.get("endpoint")
                if ep:
                    return str(ep).rstrip("/")
    except Exception:
        pass
    return DEFAULT_ENDPOINT


# ---------- HTTP ----------
def _request(method: str, path: str, *, auth: dict | None = None, body=None,
             timeout: float = 15.0, endpoint: str | None = None,
             extra_headers: dict | None = None) -> dict:
    endpoint = (endpoint or resolve_endpoint()).rstrip("/")
    url = endpoint + path
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # 网关会拦截默认 UA（Python-urllib），此处使用与 IDE/curl 一致的 UA
        "User-Agent": USER_AGENT,
        H_DOMAIN: urllib.parse.urlparse(endpoint).netloc,
    }
    if auth and auth.get("accessToken"):
        headers["Authorization"] = f"Bearer {auth['accessToken']}"
    else:
        headers[H_NO_AUTH] = "true"
        headers[H_NO_USER] = "true"
        headers[H_NO_ENTERPRISE] = "true"
        headers[H_NO_DEPARTMENT] = "true"
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        cb_log.log(f"{method} {path} -> HTTP {resp.status} ({time.time() - t0:.2f}s)", "api")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        code = None
        try:
            code = (json.loads(raw) or {}).get("code")
        except Exception:
            pass
        if code is None and e.code in (401, 403):
            code = 401
        cb_log.log(f"{method} {path} -> HTTP {e.code} code={code} "
                   f"({time.time() - t0:.2f}s) {raw[:160]}", "api")
        raise ApiError(f"HTTP {e.code}: {raw[:300]}", code=code, status=e.code) from e
    except urllib.error.URLError as e:
        cb_log.log(f"{method} {path} -> 网络错误 {e.reason} ({time.time() - t0:.2f}s)", "api")
        raise ApiError(f"网络错误：{e.reason}（{url}）") from e
    except Exception as e:  # noqa: BLE001
        cb_log.log(f"{method} {path} -> {type(e).__name__}: {e} ({time.time() - t0:.2f}s)", "api")
        raise

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _data(resp: dict):
    return (resp or {}).get("data")


def _calc_expires(auth: dict) -> None:
    now_ms = int(time.time() * 1000)
    if not auth.get("expiresAt") and auth.get("expiresIn"):
        auth["expiresAt"] = now_ms + int(auth["expiresIn"]) * 1000
    if not auth.get("refreshExpiresAt") and auth.get("refreshExpiresIn"):
        auth["refreshExpiresAt"] = now_ms + int(auth["refreshExpiresIn"]) * 1000
    auth["lastRefreshTime"] = now_ms


# ---------- 登录流程 ----------
def fetch_auth_state(endpoint: str | None = None) -> dict:
    """第 1 步：拿到 state 与浏览器登录地址。"""
    resp = _request("POST", f"/v2{PREFIX_PATH}/auth/state?platform={PLATFORM}",
                    body={}, timeout=15, endpoint=endpoint)
    data = _data(resp) or {}
    if not data.get("state") or not data.get("authUrl"):
        raise ApiError(f"获取登录地址失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
    return data


def poll_auth_token(state: str, *, timeout: float = 300.0, interval: float = 1.5,
                    endpoint: str | None = None, should_stop=None) -> dict:
    """第 3 步：轮询登录结果，返回 auth（含 accessToken/refreshToken）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop and should_stop():
            raise CanceledError("已取消登录")
        try:
            resp = _request("GET", f"/v2{PREFIX_PATH}/auth/token?state={urllib.parse.quote(state)}",
                            timeout=15, endpoint=endpoint)
            data = _data(resp) or {}
            if data.get("accessToken"):
                _calc_expires(data)
                return data
        except ApiError as e:
            if e.code not in RETRY_CODES:
                raise
        time.sleep(interval)
    raise TimeoutError("等待浏览器登录超时（5 分钟），请重试")


def poll_login_account(state: str, auth: dict, *, timeout: float = 60.0, interval: float = 1.5,
                       endpoint: str | None = None, should_stop=None) -> dict:
    """第 4 步：登录成功后取账号信息。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop and should_stop():
            raise CanceledError("已取消登录")
        try:
            resp = _request("GET", f"/v2{PREFIX_PATH}/login/account?state={urllib.parse.quote(state)}",
                            auth=auth, timeout=15, endpoint=endpoint,
                            extra_headers={H_NO_USER: "true", H_NO_ENTERPRISE: "true",
                                           H_NO_DEPARTMENT: "true"})
            data = _data(resp) or {}
            if data.get("uid"):
                return data
        except ApiError as e:
            if e.code not in RETRY_CODES:
                raise
        time.sleep(interval)
    raise TimeoutError("获取账号信息超时")


def fetch_accounts(auth: dict, endpoint: str | None = None) -> list[dict]:
    """第 5 步：该登录态下可选账号列表（个人/企业）。"""
    try:
        resp = _request("GET", f"/v2{PREFIX_PATH}/accounts", auth=auth,
                        timeout=15, endpoint=endpoint)
        return (_data(resp) or {}).get("accounts") or []
    except ApiError:
        return []


def refresh_auth(auth: dict, endpoint: str | None = None) -> dict:
    """续期登录态（refresh token 换新的 access token）。"""
    if not auth.get("refreshToken"):
        raise ApiError("该账号没有 refresh token，无法续期")
    resp = _request("POST", f"/v2{PREFIX_PATH}/auth/token/refresh", body={},
                    timeout=20, endpoint=endpoint,
                    extra_headers={H_REFRESH_TOKEN: auth["refreshToken"],
                                   H_REFRESH_SOURCE: "plugin",
                                   H_DOMAIN: auth.get("domain") or ""})
    data = _data(resp) or {}
    if not data.get("accessToken"):
        raise ApiError(f"续期失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
    if not data.get("domain"):
        data["domain"] = auth.get("domain") or ""
    _calc_expires(data)
    return data


# ---------- 额度 ----------
def fetch_payment_type(auth: dict, endpoint: str | None = None) -> str:
    try:
        resp = _request("POST", "/v2/billing/meter/get-payment-type", body={},
                        auth=auth, timeout=15, endpoint=endpoint)
        return ((_data(resp) or {}).get("paymentType") or "")
    except ApiError:
        return ""


def _num(value):
    """数值化（int/float），无效返回 0。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    return int(f) if f.is_integer() else f


def _parse_ms(value) -> int:
    """把服务端时间字段统一换算成 epoch 毫秒。

    支持 'YYYY-MM-DD HH:mm:ss'（及 'YYYY-MM-DD'、'YYYY/MM/DD …'）字符串或毫秒数值。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return 0
    s = value.strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def _ms_date(ms) -> str:
    """epoch 毫秒 -> 'YYYY-MM-DD'（本地时区）。无效返回 ''。"""
    try:
        ms = int(ms)
        if ms <= 0:
            return ""
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_quota(auth: dict, endpoint: str | None = None) -> dict:
    """查询额度：套餐 + credits 余量。

    统计口径与 IDE 内置 BackendProvider.getCurrentPlan 完全一致：
      - 请求体带 Status[valid,usedUp]/ProductCode/时间范围过滤（不带则会混入过期包，数字不准）；
      - 每个包取 CycleCapacity*Precise（周期维度）计算 total/used/left；
      - 主套餐 = 专业类包(proYear/proMon/proMonPlus) 优先，否则试用类(gift/freeMon)。
    失败时返回带 error 的字典。
    """
    out = {
        "payment_type": "", "is_pro": False, "plan_code": "", "plan_name": "",
        "usage_left": None, "usage_total": None, "usage_used": None,
        "expire_at": "", "expire_ms": 0, "refresh_ms": 0,
        "packages": [], "updated_at": "", "error": "",
    }
    try:
        out["payment_type"] = fetch_payment_type(auth, endpoint)
    except Exception as e:
        out["error"] = str(e)

    try:
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        now = datetime.now()
        far = now + timedelta(days=101 * 365)
        body = {
            "PageNumber": 1, "PageSize": 100, "ProductCode": _PRODUCT_CODE,
            "Status": [_ACCOUNT_STATUS_VALID, _ACCOUNT_STATUS_USED_UP],
            "PackageEndTimeRangeBegin": fmt(now),
            "PackageEndTimeRangeEnd": fmt(far),
        }
        resp = _request("POST", "/v2/billing/meter/get-user-resource", body=body,
                        auth=auth, timeout=20, endpoint=endpoint)
        res = ((_data(resp) or {}).get("Response") or {}).get("Data") or {}
        raw_items = res.get("Accounts") or []

        # 1) 逐包按 IDE 公式折算
        parsed = []
        for it in raw_items:
            code = it.get("PackageCode") or ""
            is_daily = code == COMMODITY_FREE
            total = _num(it.get("CycleCapacitySizePrecise") or it.get("CycleCapacitySize"))
            left = _num(it.get("CycleCapacityRemainPrecise") or it.get("CycleCapacityRemain"))
            used = max(0, total - left)
            expire_ms = _parse_ms(it.get("CycleEndTime")) if is_daily \
                else _parse_ms(it.get("DeductionEndTime") or it.get("CycleEndTime"))
            refresh_ms = _parse_ms(it.get("CycleEndTime"))
            parsed.append({
                "code": code,
                "name": it.get("PackageName") or code,
                "is_daily": is_daily,
                "total": total, "used": used, "left": left,
                "expire_ms": expire_ms,
                "refresh_ms": refresh_ms if not is_daily else 0,
                "status": it.get("Status"),
            })

        # 2) 排序与主套餐判定（与 IDE 相同）
        def group(code: str) -> int:
            if code in PRO_CODES or code in (COMMODITY_FREE_MON, COMMODITY_EXTRA):
                return 1
            if code in (COMMODITY_GIFT, COMMODITY_ACTIVITY):
                return 2
            if code == COMMODITY_FREE:
                return 3
            return 4

        parsed.sort(key=lambda p: group(p["code"]))
        main = next((p for p in parsed if p["code"] in PRO_CODES), None) \
            or next((p for p in parsed if p["code"] in TRIAL_CODES), None)

        usage_total = sum(p["total"] for p in parsed)
        usage_used = sum(p["used"] for p in parsed)
        usage_left = sum(p["left"] for p in parsed)

        out["packages"] = parsed
        out["usage_total"] = usage_total
        out["usage_used"] = usage_used
        out["usage_left"] = usage_left
        if main:
            out["is_pro"] = main["code"] in PRO_CODES
            out["plan_code"] = main["code"]
            out["plan_name"] = main["name"]
            out["expire_ms"] = main["expire_ms"] or main["refresh_ms"]
            out["expire_at"] = _ms_date(out["expire_ms"])
            out["refresh_ms"] = main["refresh_ms"]
            out["refresh_at"] = _ms_date(main["refresh_ms"])
        out["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        out["error"] = out["error"] or str(e)
    return out
