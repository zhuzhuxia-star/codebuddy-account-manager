# -*- coding: utf-8 -*-
"""多域名可用性 + 展示域名口径 夹具测试（真实起 HTTP 服务全链路）。

用户规矩（2026-11-27 二次修正）：页面展示的所有域名/接口只用他自己
的域名——当前 Host 是自有域名（koi.ipv64.de、sms.dcio.eu.org 或其他
非容器原生域名）就展示当前 Host；Host 是容器原生地址
（*.tryaltare.ai / *.altare.global）或 localhost/纯 IP 时一律回落主
展示域名 koi.ipv64.de，页面上绝不出现容器原生地址。且接口本身在任何
映射到本站的域名下都要能正常请求。覆盖：
[1] 展示口径：设置页端点、/fleet 接入地址与 curl 示例、/oos 分享短链
    按上表渲染；原生 Host 下三页无 tryaltare/altare.global 字样，非主
    域名自有 Host 下三页无写死的主展示域名。
[2] 双 Host 全链路：两个不同 Host 分别跑登录（断言跳转相对、Cookie
    无 Domain 属性即 host-only）、未登录跳转相对、一次 chat 调用、
    一次心跳上报、一次 /v1/models、一次短链访问（就地出内容不跳转）。
[3] 回落与净化：无 Host 上下文 / 空 / 非法形状 / 容器原生 / localhost /
    纯 IP 的 Host → 回落主展示域名；自有域名 Host 原样生效。
运行：python3 test_hostbase.py"""
import copy
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="cbam-hostbase-test-")
os.environ["APPDATA"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA_DIR = Path(TMP) / "CodeBuddyAccountManager"
DATA_DIR.mkdir(parents=True, exist_ok=True)

FLEET_KEY = "<redacted>"
(DATA_DIR / "fleet.json").write_text(json.dumps({
    "key_plain": FLEET_KEY,
    "key_hash": hashlib.sha256(FLEET_KEY.encode("utf-8")).hexdigest(),
    "created_at": "2026-11-27 00:00:00",
}, ensure_ascii=False, indent=2), encoding="utf-8")

import gateway  # noqa: E402
import server  # noqa: E402
from account_store import _strip_uid_prefix, build_ide_session  # noqa: E402

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


class FakeStore:
    def __init__(self, recs):
        self.recs = {r["id"]: r for r in recs}

    def list_accounts(self):
        return list(self.recs.values())

    def get(self, rid):
        return self.recs.get(rid)

    def update(self, rec):
        self.recs[rec["id"]] = rec
        return True


def make_rec(uid, label, access, refresh):
    plain = build_ide_session({
        "account": {"uid": uid, "label": label, "domain": "www.codebuddy.cn"},
        "auth": {"accessToken": _strip_uid_prefix(access),
                 "refreshToken": refresh, "domain": "www.codebuddy.cn"}})
    return {"id": uid, "uid": uid, "label": label,
            "keys": {"##current##": plain}, "in_pool": True,
            "quota": {"usage_left": 100.0}}


store = FakeStore([make_rec("u-host", "多域名测试号", "tok-host", "ref-host")])
server.AccountStore = lambda *a, **k: store  # noqa: E731

SAMPLE = json.loads(open(os.path.join(HERE, "catalog_sample.json"),
                         encoding="utf-8").read())
gateway._fetch_catalog_raw = lambda auth, uid: copy.deepcopy(SAMPLE)


def reset_cache():
    gateway._models_cache.clear()
    gateway._models_cache.update(at=0, data=None)


reset_cache()


class FakeResp:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    def readline(self):
        return self._buf.readline()

    def read(self):
        return self._buf.read()

    def close(self):
        pass


def _sse(chunks):
    lines = []
    for i, c in enumerate(chunks):
        obj = {"id": "chatcmpl-x", "object": "chat.completion.chunk",
               "created": 1700000000, "model": "glm-5.3-flash",
               "choices": [{"index": 0, "delta": {"content": c},
                            "finish_reason":
                            "stop" if i == len(chunks) - 1 else None}]}
        lines.append("data: " + json.dumps(obj, ensure_ascii=False))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


gateway._open_chat = lambda a, u, e, p, c: (
    200, FakeResp(_sse(["你", "好"])), b"")

SHORT_BODY = "短链多域名测试内容".encode("utf-8")


class FakeU3:
    def open_get(self, bucket, key, range_header=None):
        return 200, {"Content-Type": "text/plain; charset=utf-8",
                     "Content-Length": str(len(SHORT_BODY))}, \
            io.BytesIO(SHORT_BODY)


server.u3_client = lambda: FakeU3()
SHORT_CODE = server.oos_short_code("docs/hello.txt")

GW_KEY = gateway.ensure_key()
ADMIN_PW = "test-pass-123"
conf = server.load_conf()
server.set_password(conf, ADMIN_PW)
server.save_conf(conf)

srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

HOST_A = "koi.ipv64.de"
HOST_B = "se-sto11.tryaltare.ai:25536"
HOST_C = "example.test"
HOST_SMS = "sms.dcio.eu.org"
HOST_ALT = "sg-sgp06.altare.global"
KOI = "koi.ipv64.de"


def req(method, path, json_body=None, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    hdrs = dict(headers or {})
    data = raw
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, dict(resp.getheaders()), text)
    conn.close()
    return out


def login(host):
    st, hdrs, _ = req(
        "POST", "/login", raw=("password=" + ADMIN_PW).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Host": host})
    cookie = hdrs.get("Set-Cookie") or ""
    return st, hdrs, cookie


# ---------- [1] 展示口径：自有域名用当前 Host，容器原生回落主域名 ----------
print("[1] 展示口径（自有域名随 Host / 容器原生回落主展示域名）")
_, _, cookie_a = login(HOST_A)
sess = cookie_a.split(";")[0]
# (请求 Host, 期望展示的 Host)：自有域名 → 当前 Host；容器原生 → KOI
DISPLAY_CASES = [
    (HOST_A, HOST_A),
    (HOST_SMS, HOST_SMS),
    (HOST_C, HOST_C),
    (HOST_B, KOI),
    (HOST_ALT, KOI),
]
for host, disp in DISPLAY_CASES:
    st, _, html = req("GET", "/settings",
                      headers={"Cookie": sess, "Host": host})
    check(f"[{host}] 设置页 Base URL 展示 {disp}",
          f"<code>https://{disp}/v1</code>" in html)
    check(f"[{host}] 设置页 models 端点展示 {disp}",
          f"GET https://{disp}/v1/models" in html)
    check(f"[{host}] 设置页 chat 端点展示 {disp}",
          f"POST https://{disp}/v1/chat/completions" in html)
    s_html = html
    st, _, html = req("GET", "/fleet",
                      headers={"Cookie": sess, "Host": host})
    check(f"[{host}] fleet 心跳地址展示 {disp}",
          f"POST https://{disp}/api/v1/heartbeat" in html)
    check(f"[{host}] fleet 短信地址展示 {disp}",
          f"POST https://{disp}/api/v1/sms" in html)
    check(f"[{host}] fleet curl 示例展示 {disp}",
          f"curl -X POST https://{disp}/api/v1/heartbeat" in html
          and f"curl -X POST https://{disp}/api/v1/sms" in html)
    f_html = html
    st, _, html = req("GET", "/oos/link?key=docs/hello.txt",
                      headers={"Cookie": sess, "Host": host})
    check(f"[{host}] 分享短链展示 {disp}",
          f"https://{disp}/oos/s/{SHORT_CODE}" in html)
    o_html = html
    if disp != KOI:
        check(f"[{host}] 三页无写死主展示域名",
              KOI not in s_html and KOI not in f_html and KOI not in o_html)
    if host in (HOST_B, HOST_ALT):
        check(f"[{host}] 三页无容器原生地址字样",
              "tryaltare" not in s_html + f_html + o_html
              and "altare.global" not in s_html + f_html + o_html)

# ---------- [2] 双 Host 全链路 ----------
print("[2] 双 Host 全链路可用")
for host in (HOST_A, HOST_B):
    st, hdrs, cookie = login(host)
    check(f"[{host}] 登录 303 且跳转相对",
          st == 303 and (hdrs.get("Location") or "") == "/",
          f"{st} {hdrs.get('Location')}")
    check(f"[{host}] 登录 Cookie host-only（Path=/、无 Domain）",
          "cbw_session=" in cookie and "Path=/" in cookie
          and "Domain" not in cookie, cookie[:80])
    sess_h = cookie.split(";")[0]

    st, hdrs, _ = req("GET", "/fleet", headers={"Host": host})
    loc = hdrs.get("Location") or ""
    check(f"[{host}] 未登录跳转相对路径",
          st == 303 and loc.startswith("/") and "://" not in loc,
          f"{st} {loc}")

    st, _, txt = req("POST", "/v1/chat/completions",
                     {"model": "glm-5.3-flash",
                      "messages": [{"role": "user", "content": "你好"}]},
                     {"Authorization": "Bearer " + GW_KEY, "Host": host})
    check(f"[{host}] chat 调用 200 出字",
          st == 200 and "你好" in txt, f"{st} {txt[:100]}")

    st, _, txt = req("GET", "/v1/models",
                     headers={"Authorization": "Bearer " + GW_KEY,
                              "Host": host})
    check(f"[{host}] /v1/models 200", st == 200 and "glm-5.3-flash" in txt,
          f"{st}")

    st, _, txt = req("POST", "/api/v1/heartbeat",
                     {"imei": "860000000000777", "phone": "13800000000",
                      "online": True},
                     {"X-Device-Key": FLEET_KEY, "Host": host})
    check(f"[{host}] 心跳上报 200",
          st == 200 and json.loads(txt).get("ok") is True,
          f"{st} {txt[:80]}")

    st, hdrs, txt = req("GET", f"/oos/s/{SHORT_CODE}",
                        headers={"Host": host})
    check(f"[{host}] 短链就地出内容不跳转",
          st == 200 and txt == SHORT_BODY.decode("utf-8")
          and "Location" not in hdrs,
          f"{st} {hdrs.get('Location')} {txt[:40]}")

    st, _, _ = req("GET", "/fleet",
                   headers={"Cookie": sess_h, "Host": host})
    check(f"[{host}] 本域名 cookie 能打开 /fleet", st == 200, str(st))

# ---------- [3] 回落与净化（直调函数） ----------
print("[3] 回落与净化")
server.set_display_host(None)
check("无 Host 上下文回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
check("回落时参考区端点为主展示域名",
      "<code>https://koi.ipv64.de/v1</code>" in server._gw_info_html())
server.set_display_host("")
check("空 Host 回落", server.display_base() == "https://koi.ipv64.de")
server.set_display_host("bad host!!<script>")
check("非法 Host 被净化回落",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("example.test")
check("合法自有域名 Host 生效", server.display_base() == "https://example.test")
server.set_display_host("sms.dcio.eu.org")
check("sms 自有域名生效",
      server.display_base() == "https://sms.dcio.eu.org",
      server.display_base())
server.set_display_host("se-sto11.tryaltare.ai:25536")
check("tryaltare 容器地址（带端口）回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("se-sto11.tryaltare.ai")
check("tryaltare 容器地址（无端口）回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("sg-sgp06.altare.global")
check("altare.global 容器地址回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("127.0.0.1:25536")
check("纯 IPv4 Host 回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("localhost:8080")
check("localhost 回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host("[::1]:8080")
check("IPv6 Host 回落主展示域名",
      server.display_base() == "https://koi.ipv64.de",
      server.display_base())
server.set_display_host(None)

srv.shutdown()
print(f"\n== hostbase: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
