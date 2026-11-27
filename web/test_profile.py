# -*- coding: utf-8 -*-
"""client_profile 画像夹具测试：mock urlopen 抓每条出站请求，逐项断言
请求头与官方客户端原始口径一致（UA/公共头/链路头/身份头），并验证额度
到期时刻的包型口径。运行：python3 test_profile.py"""
import gzip
import json
import os
import sys
import tempfile
import urllib.request

TMP = tempfile.mkdtemp(prefix="cbam-profile-test-")
os.environ["APPDATA"] = TMP
os.environ["CB_API_ENDPOINT"] = "https://copilot.tencent.com"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 先落一份假的 gateway_devices.json，验证设备基线同源读取
_dev_dir = os.path.join(TMP, "CodeBuddyAccountManager")
os.makedirs(_dev_dir, exist_ok=True)
with open(os.path.join(_dev_dir, "gateway_devices.json"), "w",
          encoding="utf-8") as f:
    json.dump({"acct-1": {"machine_id": "MACH-1", "session_id": "sess-1",
                          "platform": "darwin", "client_platform": "macos",
                          "stainless_os": "MacOS", "stainless_arch": "arm64",
                          "os_version": "24.6.0", "cpu_model": "Apple M2",
                          "cpu_cores": 8, "memory_gb": 16}}, f)

import cb_api  # noqa: E402
import client_profile  # noqa: E402
import wb_api  # noqa: E402
import wb_extra  # noqa: E402
import wb_growth  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


CAPTURED = []
RESPONSES = []


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._raw = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self, *a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    CAPTURED.append(req)
    return RESPONSES.pop(0)


urllib.request.urlopen = fake_urlopen


def hdrs(req):
    return {k.lower(): v for k, v in req.headers.items()}


def queue(payload, **kw):
    RESPONSES.append(FakeResp(payload, **kw))


AUTH = {"accessToken": "acc-old", "refreshToken": "ref-old",
        "domain": "www.codebuddy.cn"}
client_profile.bind_auth(AUTH, {"uid": "u123", "enterprise_id": "",
                                "department": ""})

print("== 设备基线（与 gateway_devices.json 同源） ==")
base = client_profile.device_baseline()
check("stainless_os 从设备文件读", base["stainless_os"] == "MacOS")
check("client_platform 从设备文件读", base["client_platform"] == "macos")

print("== 刷新令牌（框架类 + 根 span 链路头） ==")
queue({"code": 0, "data": {"accessToken": "acc-new", "refreshToken": "ref-new",
                           "expiresIn": 100, "refreshExpiresIn": 200,
                           "domain": "www.codebuddy.cn"}})
new_auth = cb_api.refresh_auth(dict(AUTH))
h = hdrs(CAPTURED[-1])
check("UA 是后台服务 UA", h.get("user-agent") == client_profile.DAEMON_UA,
      h.get("user-agent"))
check("Accept 框架口径", h.get("accept") == "application/json, text/plain, */*")
check("X-Requested-With", h.get("x-requested-with") == "XMLHttpRequest")
check("X-Product SaaS", h.get("x-product") == "SaaS")
check("X-Client-Version", h.get("x-client-version") == "5.11.3")
check("X-Client-Platform", h.get("x-client-platform") == "macos")
check("X-Client-Product", h.get("x-client-product") == "WorkBuddy")
check("X-Request-ID 32hex", len(h.get("x-request-id", "")) == 32
      and all(c in "0123456789abcdef" for c in h["x-request-id"]))
check("Authorization 带旧 access", h.get("authorization") == "Bearer acc-old")
check("X-User-Id 来自身份绑定", h.get("x-user-id") == "u123")
check("X-Domain 是登录域", h.get("x-domain") == "www.codebuddy.cn")
check("X-Refresh-Token", h.get("x-refresh-token") == "ref-old")
check("X-Auth-Refresh-Source", h.get("x-auth-refresh-source") == "plugin")
check("无 X-No-*", not any(k.startswith("x-no-") for k in h))
check("X-Trace-ID 32hex", len(h.get("x-trace-id", "")) == 32)
check("b3 根 span 形（-1- 结尾）", h.get("b3", "").endswith("-1-")
      and h["b3"].startswith(h["x-trace-id"]))
check("X-B3-ParentSpanId 空值", h.get("x-b3-parentspanid") == "")
check("X-B3-Sampled", h.get("x-b3-sampled") == "1")
check("身份绑定传给新令牌",
      (client_profile.identity_for(new_auth) or {}).get("uid") == "u123")

print("== 积分（AuthService 类） ==")
queue({"code": 0, "data": {"paymentType": "free"}})
pt = cb_api.fetch_payment_type(dict(AUTH))
h = hdrs(CAPTURED[-1])
check("UA axios/1.16.1", h.get("user-agent") == "axios/1.16.1")
check("Accept application/json", h.get("accept") == "application/json")
check("Accept-Language zh（积分查询）", h.get("accept-language") == "zh")
check("无 X-Requested-With", "x-requested-with" not in h)
check("无 X-Product", "x-product" not in h)
check("X-User-Id", h.get("x-user-id") == "u123")
check("paymentType 读到", pt == "free")

_FMT = "%Y-%m-%d %H:%M:%S"
import time as _time


def _ms(s):
    return int(_time.mktime(_time.strptime(s, _FMT)) * 1000)


queue({"code": 0, "data": {"paymentType": "pro"}})
queue({"code": 0, "data": {"Response": {"Data": {"Accounts": [
    {"PackageCode": "TCACA_code_002_AkiJS3ZHF5", "PackageName": "专业版月包",
     "CycleCapacitySizePrecise": "1000", "CycleCapacityRemainPrecise": "400",
     "CycleEndTime": "2027-01-01 00:00:00",
     "DeductionEndTime": "2035-01-01 00:00:00", "Status": 0},
    {"PackageCode": "TCACA_code_007_nzdH5h4Nl0", "PackageName": "活动包",
     "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "50",
     "CycleEndTime": "2027-06-01 00:00:00",
     "DeductionEndTime": "2027-03-01 00:00:00", "Status": 0},
]}}}})
q = cb_api.fetch_quota(dict(AUTH))
pkgs = {p["code"]: p for p in q["packages"]}
check("订阅包到期=CycleEndTime（官方刷新口径）",
      pkgs["TCACA_code_002_AkiJS3ZHF5"]["expire_ms"] == _ms("2027-01-01 00:00:00"),
      str(pkgs["TCACA_code_002_AkiJS3ZHF5"]["expire_ms"]))
check("一次性包到期=DeductionEndTime",
      pkgs["TCACA_code_007_nzdH5h4Nl0"]["expire_ms"] == _ms("2027-03-01 00:00:00"))
check("额度合计用 Precise 原值", q["usage_left"] == 450
      and q["usage_total"] == 1100)

print("== 签到（AuthService 类，无 Accept-Language） ==")
queue({"code": 0, "data": {"active": True, "today_checked_in": False}})
st = wb_api.fetch_status(dict(AUTH))
h = hdrs(CAPTURED[-1])
check("UA axios", h.get("user-agent") == "axios/1.16.1")
check("无 Accept-Language", "accept-language" not in h)
check("状态读到", st.get("ok") is True)
queue({"code": 0, "data": {"credit": 100, "streak_days": 3}})
res = wb_api.checkin({"accessToken": "acc-x", "refreshToken": "ref-x",
                      "domain": "www.codebuddy.cn"})
h = hdrs(CAPTURED[-1])
check("未绑定身份 X-User-Id 发空串", h.get("x-user-id") == "")
check("签到成功", res.get("ok") is True and res.get("credit") == 100)

print("== 成长（框架类） ==")
queue({"code": 0, "data": {"tasks": []}})
wb_growth.fetch_tasks(dict(AUTH))
h = hdrs(CAPTURED[-1])
check("UA 后台服务", h.get("user-agent") == client_profile.DAEMON_UA)
check("X-Product SaaS", h.get("x-product") == "SaaS")
check("GET 无 Content-Type", "content-type" not in h)

print("== 活跃上报（框架 + charset Content-Type） ==")
queue({"code": 0})
rep = wb_extra.report_activity(dict(AUTH), "u123", "昵称")
h = hdrs(CAPTURED[-1])
check("Content-Type 带 charset",
      h.get("content-type") == "application/json;charset=UTF-8",
      h.get("content-type"))
check("UA 后台服务", h.get("user-agent") == client_profile.DAEMON_UA)
check("X-User-Id 是上报 uid", h.get("x-user-id") == "u123")
check("上报成功", rep.get("ok") is True)

print("== 登录流程（框架 + 登录链路，轮询共用子 span） ==")
queue({"code": 0, "data": {"state": "st1", "authUrl": "https://example.com/a"}})
info = cb_api.fetch_auth_state()
h1 = hdrs(CAPTURED[-1])
check("X-Custom-Channel-Id unknown", h1.get("x-custom-channel-id") == "unknown")
check("四个 X-No-* 齐全", all(h1.get(k) == "true" for k in
      ("x-no-authorization", "x-no-user-id", "x-no-enterprise-id",
       "x-no-department-info")))
check("带 traceparent", "traceparent" in h1)
queue({"code": 0, "data": {"accessToken": "a1", "refreshToken": "r1",
                           "expiresIn": 10, "refreshExpiresIn": 20}})
cb_api.poll_auth_token("st1")
h2 = hdrs(CAPTURED[-1])
check("轮询与发起同 trace id", h2.get("x-trace-id") == h1.get("x-trace-id"))
check("traceparent 同 trace id",
      h2.get("traceparent", "").split("-")[1] == h1.get("x-trace-id"))
check("子 span 与发起的不同",
      h2.get("traceparent") != h1.get("traceparent"))

print("== 网页会话：登录码（Node fetch 形） ==")
queue({"code": 0, "data": {"deviceCode": "dc1"}})
code = wb_extra._fetch_device_code("https://www.workbuddy.cn", dict(AUTH), "u123")
h = hdrs(CAPTURED[-1])
check("UA node", h.get("user-agent") == "node")
check("Accept */*", h.get("accept") == "*/*")
check("带 X-Refresh-Token", h.get("x-refresh-token") == "ref-old")
check("登录码读到", code == "dc1")

print("== 网页 XHR（Cookie 类） ==")
queue({"code": 0, "data": {"x": 1}})
out = wb_extra._web_once("https://www.workbuddy.cn",
                         [{"name": "session", "value": "v",
                           "domain": "www.workbuddy.cn", "path": "/"}],
                         "POST", "/billing/meter/compensation-status",
                         "/profile/usage", None)
h = hdrs(CAPTURED[-1])
check("X-Client-Platform web", h.get("x-client-platform") == "web")
check("Sec-CH-UA-Platform", h.get("sec-ch-ua-platform") == "\"macOS\"")
check("不带 Authorization", "authorization" not in h)
check("Referer 是页面", h.get("referer", "").endswith("/profile/usage"))
check("data 读到", out == {"x": 1})

print("== 解压兜底 ==")
raw = gzip.compress(json.dumps({"code": 0}).encode("utf-8"))
check("gzip 解压", client_profile.decode_body(raw, "gzip")
      == json.dumps({"code": 0}).encode("utf-8"))

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
