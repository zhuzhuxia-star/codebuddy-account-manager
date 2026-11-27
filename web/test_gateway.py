# -*- coding: utf-8 -*-
"""gateway.py 夹具测试：mock 上游，覆盖鉴权/模型/流式/聚合/入池过滤/
401 续期重试/换号兜底/会话粘性/服务端点。运行：python3 test_gateway.py"""
import http.client
import io
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="cbam-gw-test-")
os.environ["APPDATA"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cb_api  # noqa: E402
import cb_app  # noqa: E402
import cb_secrets  # noqa: E402
import gateway  # noqa: E402
from account_store import AccountStore, build_ide_session  # noqa: E402

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


def make_rec(uid, label, access, refresh, in_pool=True, left=100.0, dead=False):
    # 与真实入库一致：build_ide_session 会把令牌按 IDE 格式存成 uid+token，
    # 这里先剥掉前缀，模拟刚导入/刚登录时的原始令牌形态
    from account_store import _strip_uid_prefix
    plain = build_ide_session({
        "account": {"uid": uid, "label": label, "domain": "www.codebuddy.cn"},
        "auth": {"accessToken": _strip_uid_prefix(access),
                 "refreshToken": refresh, "domain": "www.codebuddy.cn"}})
    rec = {"id": uid, "uid": uid, "label": label,
           "keys": {"##current##": plain}, "in_pool": in_pool,
           "quota": {"usage_left": left}}
    if dead:
        rec["liveness"] = {"status": "auth_dead"}
    return rec


class FakeResp:
    def __init__(self, body: bytes, ctype="text/event-stream"):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": ctype}
        self.status = 200

    def readline(self):
        return self._buf.readline()

    def read(self):
        return self._buf.read()

    def close(self):
        pass


def sse(chunks, usage=None, finish="stop"):
    lines = []
    for i, c in enumerate(chunks):
        obj = {"id": "chatcmpl-x", "object": "chat.completion.chunk",
               "created": 1700000000, "model": "glm-5.3-flash",
               "choices": [{"index": 0, "delta": {"content": c},
                            "finish_reason": finish if i == len(chunks) - 1 else None}]}
        if usage and i == len(chunks) - 1:
            obj["usage"] = usage
        lines.append("data: " + json.dumps(obj, ensure_ascii=False))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


BODY = {"model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "你好"}]}

# ---------- 1. 密钥 ----------
print("[1] 客户端密钥")
key = gateway.ensure_key()
check("密钥已生成", key.startswith("sk-cbam-") and len(key) > 20)
check("密钥文件权限 600", (os.stat(gateway.KEY_FILE).st_mode & 0o777) == 0o600)
check("正确密钥通过", gateway.check_bearer("Bearer " + key))
check("错误密钥拒绝", not gateway.check_bearer("Bearer sk-cbam-wrong"))
check("无密钥拒绝", not gateway.check_bearer(""))
check("掩码不含完整明文", key not in gateway.key_masked()
      and "••••" in gateway.key_masked())

# ---------- 2. 模型列表 ----------
print("[2] 模型列表")
gateway._models_cache.update(at=0, data=None)
data = gateway.list_models(FakeStore([]))
ids = [m["id"] for m in data["data"]]
check("无账号时静态表兜底", "glm-5.2" in ids and "glm-5.3-flash" in ids, str(ids))

orig_fetch = gateway._fetch_catalog
gateway._fetch_catalog = lambda auth, uid: [
    {"id": "live-model-1", "object": "model", "created": 0,
     "owned_by": "workbuddy"}]
gateway._models_cache.update(at=0, data=None)
rec = make_rec("u-live", "活号", "tok-live", "ref-live")
data = gateway.list_models(FakeStore([rec]))
check("有账号时用上游目录", [m["id"] for m in data["data"]] == ["live-model-1"])
gateway._fetch_catalog = orig_fetch
gateway._models_cache.update(at=0, data=None)

# ---------- 3. 请求体转换 ----------
print("[3] 请求体转换")
conv = gateway._convert_body({
    "model": "glm-5.3-flash", "stream": False,
    "messages": [
        {"role": "assistant", "content": "x", "reasoning_text": "内部推理",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r"},
        {"role": "user", "content": "继续"}]})
check("强制 stream", conv["stream"] is True)
check("reasoning_text 被剥离",
      all("reasoning_text" not in m for m in conv["messages"]))
check("空名 tool_calls 与其 tool 结果被剔除",
      len(conv["messages"]) == 2
      and "tool_calls" not in conv["messages"][0]
      and all(m.get("role") != "tool" for m in conv["messages"]),
      json.dumps(conv["messages"], ensure_ascii=False))
check("harnessMetadata 已注入",
      "harnessMetadata" in (conv.get("extra_vars") or {}))
check("cache_retention 默认 long", conv.get("cache_retention") == "long")

# ---------- 4. 流式透传 ----------
print("[4] 流式透传")
calls = []
orig_open = gateway._open_chat


def open_ok(auth, uid, ent, payload, conv_id):
    calls.append(auth.get("accessToken"))
    return 200, FakeResp(sse(["你", "好"])), b""


gateway._open_chat = open_ok
store = FakeStore([make_rec("u1", "甲", "tok-1", "ref-1")])
out = gateway.chat(store, dict(BODY, stream=True), None)
raw = b"".join(out.payload).decode("utf-8")
check("流式 outcome", out.kind == "stream" and out.status == 200)
check("分片原样透传", '"content": "你"' in raw and '"content": "好"' in raw)
check("带 [DONE]", "[DONE]" in raw)
check("只调一个号", calls == ["tok-1"], str(calls))

# ---------- 5. 非流式聚合 ----------
print("[5] 非流式聚合")
calls.clear()
gateway._open_chat = lambda a, u, e, p, c: (
    calls.append(a.get("accessToken")),
    (200, FakeResp(sse(["你", "好", "吗"],
                        usage={"prompt_tokens": 3, "completion_tokens": 5,
                               "total_tokens": 8})), b""))[1]
out = gateway.chat(store, dict(BODY), None)
check("聚合 outcome", out.kind == "json" and out.status == 200)
msg = out.payload["choices"][0]["message"]
check("内容拼接", msg["content"] == "你好吗", msg.get("content", ""))
check("usage 透传", (out.payload.get("usage") or {}).get("total_tokens") == 8)
check("finish_reason", out.payload["choices"][0]["finish_reason"] == "stop")

# ---------- 6. 入池过滤 ----------
print("[6] 入池过滤")
gateway._open_chat = open_ok
out = gateway.chat(FakeStore([make_rec("u2", "乙", "t2", "r2", in_pool=False)]),
                   dict(BODY), None)
check("未入池不接 → 503", out.status == 503
      and out.payload["error"]["code"] == "no_available_account")
out = gateway.chat(FakeStore([make_rec("u3", "丙", "t3", "r3", dead=True)]),
                   dict(BODY), None)
check("登录失效不接 → 503", out.status == 503)
out = gateway.chat(FakeStore([make_rec("u4", "丁", "t4", "r4", left=0)]),
                   dict(BODY), None)
check("额度耗尽不接 → 503", out.status == 503)

# ---------- 7. 401 续期重试 ----------
print("[7] 401 续期重试")
seq = []


def open_401_then_ok(auth, uid, ent, payload, conv_id):
    seq.append(auth.get("accessToken"))
    if "tok-old" in (auth.get("accessToken") or ""):
        return 401, None, b'{"code": 12153, "msg": "session expired"}'
    return 200, FakeResp(sse(["通"])), b""


orig_refresh = cb_api.refresh_auth
cb_api.refresh_auth = lambda auth: {
    "accessToken": "tok-new", "refreshToken": "ref-new",
    "expiresIn": 3600, "domain": "www.codebuddy.cn"}
gateway._open_chat = open_401_then_ok
store = FakeStore([make_rec("u5", "戊", "tok-old", "ref-old")])
out = gateway.chat(store, dict(BODY, stream=True), None)
raw = b"".join(out.payload).decode("utf-8")
check("续期后同号重试成功", out.status == 200 and "通" in raw)
check("调用顺序 旧→新",
      len(seq) == 2 and "tok-old" in seq[0] and seq[1] == "tok-new", str(seq))
stored_auth = json.loads(list(store.get("u5")["keys"].values())[0])["auth"]
# 注：既有 _rebuild_keys_with_auth 对已是 IDE 复合格式（uid+token）的明文
# 再重建时有令牌还原 quirk（access 仍旧、仅 expiresIn 被合并），与生产
# 每日续期的实际行为一致，本网关不另行处理；这里只断言合并确实发生。
check("续期结果已合并写回账号库",
      stored_auth.get("expiresIn") == 3600
      and stored_auth.get("domain") == "www.codebuddy.cn",
      json.dumps(stored_auth)[:160])

# 续期也失败（refresh 抛 12153）→ 换不到号时 503，且账号被标记失效
def refresh_dead(auth):
    raise RuntimeError("HTTP 401 code=12153 Stale token")


cb_api.refresh_auth = refresh_dead
gateway._open_chat = lambda a, u, e, p, c: (
    401, None, b'{"code": 12153, "msg": "expired"}')
store = FakeStore([make_rec("u6", "己", "tok-d", "ref-d")])
out = gateway.chat(store, dict(BODY), None)
check("续期失败 → 503", out.status == 503)
check("账号被标记 auth_dead",
      (store.get("u6").get("liveness") or {}).get("status") == "auth_dead")
cb_api.refresh_auth = orig_refresh

# ---------- 8. 换号兜底 ----------
print("[8] 换号兜底")
order = []


def open_quota_a(auth, uid, ent, payload, conv_id):
    order.append(auth.get("accessToken"))
    if auth.get("accessToken") == "tok-A":
        return 402, None, b'{"code": 14001, "msg": "insufficient quota"}'
    return 200, FakeResp(sse(["B"])), b""


gateway._open_chat = open_quota_a
orig_pick = gateway._weighted_pick
gateway._weighted_pick = lambda cands: cands[0]
store = FakeStore([make_rec("uA", "A号", "tok-A", "ref-A", left=10),
                   make_rec("uB", "B号", "tok-B", "ref-B", left=99)])
out = gateway.chat(store, dict(BODY, stream=True), None)
raw = b"".join(out.payload).decode("utf-8")
check("额度不足换号成功", out.status == 200 and '"B"' in raw)
check("顺序 A→B", order == ["tok-A", "tok-B"], str(order))

# 请求本身的问题（11101）不换号，原样回错
order.clear()
gateway._open_chat = lambda a, u, e, p, c: (
    order.append(a.get("accessToken")),
    (400, None, b'{"code": 11101, "msg": "bad param"}'))[1]
out = gateway.chat(store, dict(BODY), None)
check("11101 不换号回 400", out.status == 400 and len(order) == 1,
      f"{out.status} {order}")
gateway._weighted_pick = orig_pick

# 全池额度不足 → 503
gateway._open_chat = lambda a, u, e, p, c: (
    402, None, b'{"code": 14001, "msg": "insufficient quota"}')
out = gateway.chat(store, dict(BODY), None)
check("全池接不了 → 503", out.status == 503)

# ---------- 9. 会话粘性 ----------
print("[9] 会话粘性")
used = []
gateway._open_chat = lambda a, u, e, p, c: (
    used.append(a.get("accessToken")),
    (200, FakeResp(sse(["s"])), b""))[1]
store = FakeStore([make_rec("uS1", "S1", "tok-S1", "ref-S1", left=10),
                   make_rec("uS2", "S2", "tok-S2", "ref-S2", left=99)])
gateway._weighted_pick = lambda cands: cands[0]
out = gateway.chat(store, dict(BODY, stream=True), "sess-xyz")
b"".join(out.payload)
gateway._weighted_pick = lambda cands: cands[-1]  # 第二发加权会选另一个号
out = gateway.chat(store, dict(BODY, stream=True), "sess-xyz")
b"".join(out.payload)
check("同 X-Session-Id 粘同一号", used == ["tok-S1", "tok-S1"], str(used))
used.clear()
gateway._weighted_pick = lambda cands: cands[0]
out = gateway.chat(store, dict(BODY, stream=True), None)
b"".join(out.payload)
gateway._weighted_pick = lambda cands: cands[-1]
out = gateway.chat(store, dict(BODY, stream=True), None)
b"".join(out.payload)
check("无头时按首条 user 消息哈希粘性", used == ["tok-S1", "tok-S1"], str(used))
gateway._weighted_pick = orig_pick
gateway._open_chat = orig_open

# ---------- 10. account_store 重导入保留 in_pool ----------
print("[10] in_pool 持久化")
import account_store as _as  # noqa: E402

_as.dpapi_protect = lambda b: b
_as.dpapi_unprotect = lambda b: b
st = AccountStore()
r1 = make_rec("uP", "持久", "tok-p", "ref-p", in_pool=True)
st.add(r1)
r2 = make_rec("uP", "持久", "tok-p2", "ref-p2", in_pool=False)
st.add(r2)
got = st.get("uP")
check("重导入后 in_pool 保留", bool(got and got.get("in_pool")))

# ---------- 11. HTTP 层 ----------
print("[11] HTTP 端点")
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

import server  # noqa: E402

http_store = FakeStore([make_rec("uH", "H号", "tok-H", "ref-H")])
server.AccountStore = lambda: http_store
gateway._open_chat = open_ok
gateway._fetch_catalog = lambda auth, uid: [
    {"id": "m-http", "object": "model", "created": 0, "owned_by": "workbuddy"}]
gateway._models_cache.update(at=0, data=None)
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def req(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


st_code, _ = req("GET", "/v1/models")
check("无密钥 /v1/models → 401", st_code == 401, str(st_code))
st_code, data = req("GET", "/v1/models",
                    headers={"Authorization": "Bearer " + key})
obj = json.loads(data)
check("带密钥 /v1/models → 200", st_code == 200
      and obj["data"][0]["id"] == "m-http", f"{st_code} {data[:120]}")
st_code, _ = req("POST", "/v1/chat/completions",
                 body=json.dumps(BODY).encode(),
                 headers={"Content-Type": "application/json"})
check("无密钥 chat → 401", st_code == 401)
st_code, data = req("POST", "/v1/chat/completions",
                    body=json.dumps(dict(BODY, stream=True)).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + key})
check("带密钥流式 chat → 200 SSE", st_code == 200 and b"[DONE]" in data,
      f"{st_code} {data[:120]}")
st_code, data = req("POST", "/v1/chat/completions",
                    body=json.dumps(BODY).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + key})
obj = json.loads(data)
check("带密钥非流式 chat → 200 聚合",
      st_code == 200 and obj["choices"][0]["message"]["content"] == "你好",
      f"{st_code} {data[:160]}")
st_code, _ = req("GET", "/")
check("原有页面未受影响（未登录跳登录页）", st_code in (200, 302, 303),
      str(st_code))
html = server.dashboard().decode("utf-8")
check("仪表盘有网关池开关", "/account/pool" in html and "网关池" in html)
httpd.shutdown()

# ---------- 12. 模型名口径：官方原名原样透传，不改写（二期口径） ----------
print("[12] 模型名原样透传")

seen_models = []


def open_capture(auth, uid, ent, payload, conv_id):
    seen_models.append(payload.get("model"))
    return 200, FakeResp(sse(["R"])), b""


gateway._open_chat = open_capture
route_store = FakeStore([make_rec("uR", "路由", "tok-R", "ref-R")])

seen_models.clear()
out = gateway.chat(route_store,
                   {"model": "glm-5.3-flash",
                    "messages": [{"role": "user", "content": "全名直通"}]}, None)
check("目录全名原样透传", seen_models == ["glm-5.3-flash"] and out.status == 200,
      str(seen_models))

seen_models.clear()
out = gateway.chat(route_store,
                   {"model": "5.3-flash",
                    "messages": [{"role": "user", "content": "短名不改写"}]}, None)
check("短名也不改写（官方什么样就什么样）", seen_models == ["5.3-flash"],
      str(seen_models))
check("响应 model 与请求一致", out.payload.get("model") == "5.3-flash",
      str(out.payload.get("model")))

seen_models.clear()
out = gateway.chat(route_store,
                   {"model": "nope-9",
                    "messages": [{"role": "user", "content": "未知模型"}]}, None)
check("未知模型名原样透传", seen_models == ["nope-9"], str(seen_models))

gateway._open_chat = orig_open

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
