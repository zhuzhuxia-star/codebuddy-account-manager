# -*- coding: utf-8 -*-
"""网关二期夹具测试：/v1/messages、/v1/responses 翻译（双向、流式+
非流式、工具 round-trip）、x-api-key 鉴权、出站官方画像断言（mock
urlopen 抓真实出站请求逐项核对）、入口容灾、设备身份持久化。
运行：python3 test_phase2.py"""
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="cbam-gw2-test-")
os.environ["APPDATA"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cb_api  # noqa: E402
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


class FakeResp:
    def __init__(self, body: bytes, ctype="text/event-stream"):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": ctype}
        self.status = 200

    def readline(self):
        return self._buf.readline()

    def read(self, n=-1):
        return self._buf.read() if n is None or n < 0 else self._buf.read(n)

    def close(self):
        pass


def sse_bytes(chunks) -> bytes:
    out = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
                  for c in chunks)
    return (out + "data: [DONE]\n\n").encode("utf-8")


def text_sse(text="你好"):
    return sse_bytes([
        {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "delta": {"role": "assistant",
                                            "content": text},
                      "finish_reason": None}]},
        {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 9, "completion_tokens": 2,
                   "total_tokens": 11}},
    ])


def tool_sse(call_id="call_1", name="get_weather"):
    return sse_bytes([
        {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
             {"index": 0, "id": call_id, "type": "function",
              "function": {"name": name, "arguments": "{\"ci"}}]},
             "finish_reason": None}]},
        {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "delta": {"tool_calls": [
             {"index": 0, "function": {"arguments": "ty\": \"北京\"}"}}]},
             "finish_reason": None}]},
        {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                   "total_tokens": 15}},
    ])


# ---------- 1. Anthropic 请求翻译 ----------
print("[1] Anthropic -> 内部 chat")
abody = {
    "model": "glm-5.3-flash", "max_tokens": 1024,
    "system": [{"type": "text", "text": "你是助手"}],
    "messages": [
        {"role": "user", "content": "北京天气？"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我查一下"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "北京"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "晴 25 度"},
            {"type": "text", "text": "那穿什么？"}]},
    ],
    "tools": [{"name": "get_weather", "description": "查天气",
               "input_schema": {"type": "object",
                                "properties": {"city": {"type": "string"}}}},
              {"type": "web_search_20250305", "name": "web_search"}],
    "tool_choice": {"type": "any"},
    "thinking": {"type": "enabled", "budget_tokens": 8000},
    "metadata": {"user_id": "user_x_session_y"},
}
chat_body, hint = gateway._anthropic_to_chat(abody)
msgs = chat_body["messages"]
check("system 块转 system 消息", msgs[0] == {"role": "system", "content": "你是助手"},
      str(msgs[0]))
check("tool_use 转 tool_calls",
      msgs[2]["tool_calls"][0]["function"]["name"] == "get_weather"
      and json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"},
      str(msgs[2]))
check("tool_result 转 tool 消息且在文本前",
      msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "toolu_1"
      and msgs[3]["content"] == "晴 25 度" and msgs[4]["content"] == "那穿什么？",
      str(msgs[3:]))
check("服务端工具被丢弃、函数工具保留",
      len(chat_body["tools"]) == 1
      and chat_body["tools"][0]["function"]["parameters"]["type"] == "object")
check("tool_choice any -> required", chat_body.get("tool_choice") == "required")
check("thinking 预算 8000 -> medium", chat_body.get("reasoning_effort") == "medium",
      str(chat_body.get("reasoning_effort")))
check("metadata.user_id 只做内部粘性提示",
      (hint or "").startswith("anthropic:") and "user_id" not in json.dumps(chat_body))

# ---------- 2. Anthropic 回复翻译（非流式） ----------
print("[2] 内部 chat -> Anthropic（非流式）")
comp = {"id": "chatcmpl-abc", "created": 1, "model": "glm-5.3-flash",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": "查到了",
                                 "tool_calls": [{
                                     "id": "call_1", "type": "function",
                                     "function": {"name": "get_weather",
                                                  "arguments": "{\"city\": \"北京\"}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15}}
am = gateway._chat_to_anthropic(comp, "glm-5.3-flash")
check("message 骨架", am["type"] == "message" and am["role"] == "assistant"
      and am["id"].startswith("msg_"), str(am)[:120])
check("text + tool_use 块",
      am["content"][0] == {"type": "text", "text": "查到了"}
      and am["content"][1]["type"] == "tool_use"
      and am["content"][1]["input"] == {"city": "北京"}, str(am["content"]))
check("stop_reason tool_use", am["stop_reason"] == "tool_use")
check("usage 映射", am["usage"] == {"input_tokens": 10, "output_tokens": 5})

# ---------- 3. Anthropic 流式翻译 ----------
print("[3] Anthropic 流式事件序列")
mixed = sse_bytes([
    {"id": "chatcmpl-t", "created": 1, "model": "glm-5.3-flash",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "查"},
                  "finish_reason": None}]},
])[:-len("data: [DONE]\n\n")] + tool_sse()
raw = b"".join(gateway._anthropic_stream(
    iter([mixed]), "glm-5.3-flash")).decode("utf-8")
events = re.findall(r"^event: (.+)$", raw, re.M)
check("事件序列符合 Anthropic 规范",
      events == ["message_start", "content_block_start",
                 "content_block_delta", "content_block_stop",
                 "content_block_start", "content_block_delta",
                 "content_block_delta", "content_block_stop",
                 "message_delta", "message_stop"], str(events))
check("tool_use 块带 id/name",
      '"type": "tool_use"' in raw and '"id": "call_1"' in raw
      and '"name": "get_weather"' in raw)
check("input_json_delta 拼参数", '"partial_json": "{\\"ci' in raw)
md = [json.loads(l[6:]) for l in raw.splitlines()
      if l.startswith("data: ") and '"message_delta"' in l]
check("message_delta stop_reason=tool_use 且带 output_tokens",
      md and md[0]["delta"]["stop_reason"] == "tool_use"
      and md[0]["usage"]["output_tokens"] == 5, str(md))
raw2 = b"".join(gateway._anthropic_stream(
    iter([text_sse("你好")]), "glm-5.3-flash")).decode("utf-8")
ev2 = re.findall(r"^event: (.+)$", raw2, re.M)
check("纯文本流事件序列",
      ev2 == ["message_start", "content_block_start", "content_block_delta",
              "content_block_stop", "message_delta", "message_stop"], str(ev2))
check("text_delta 内容", '"text_delta"' in raw2 and "你好" in raw2)

# ---------- 4. Responses 请求翻译 ----------
print("[4] Responses -> 内部 chat")
rbody = {
    "model": "glm-5.3-flash", "instructions": "你是助手",
    "input": [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "改一下这个文件"}]},
        {"type": "function_call", "call_id": "call_f", "name": "read_file",
         "arguments": "{\"path\": \"a.txt\"}"},
        {"type": "function_call_output", "call_id": "call_f",
         "output": "文件内容"},
        {"type": "custom_tool_call", "call_id": "call_p", "name": "apply_patch",
         "input": "*** Begin Patch\n*** End Patch"},
        {"type": "reasoning", "summary": []},
    ],
    "tools": [{"type": "function", "name": "read_file", "description": "读文件",
               "parameters": {"type": "object", "properties": {}}},
              {"type": "custom", "name": "apply_patch", "description": "打补丁"},
              {"type": "web_search"}],
    "reasoning": {"effort": "high"},
    "max_output_tokens": 2048,
    "prompt_cache_key": "sess-codex-1",
    "store": False,
}
chat_body, custom, hint = gateway._responses_to_chat(rbody)
msgs = chat_body["messages"]
check("instructions 转 system", msgs[0] == {"role": "system", "content": "你是助手"})
check("function_call 转 tool_calls",
      msgs[2]["tool_calls"][0]["function"]["name"] == "read_file"
      and msgs[2]["tool_calls"][0]["id"] == "call_f", str(msgs[2]))
check("function_call_output 转 tool 消息",
      msgs[3]["role"] == "tool" and msgs[3]["content"] == "文件内容")
check("custom_tool_call 转函数调用（input 字符串入参）",
      json.loads(msgs[4]["tool_calls"][0]["function"]["arguments"])
      == {"input": "*** Begin Patch\n*** End Patch"}, str(msgs[4]))
check("自定义工具名集合", custom == {"apply_patch"}, str(custom))
check("web_search 内置工具丢弃、工具数 2", len(chat_body["tools"]) == 2)
check("reasoning.effort -> reasoning_effort",
      chat_body.get("reasoning_effort") == "high")
check("max_output_tokens -> max_tokens", chat_body.get("max_tokens") == 2048)
check("prompt_cache_key 只做内部粘性提示",
      (hint or "").startswith("responses:")
      and "prompt_cache_key" not in json.dumps(chat_body))

# ---------- 5. Responses 回复翻译（非流式） ----------
print("[5] 内部 chat -> Responses（非流式）")
comp2 = {"id": "chatcmpl-z", "created": 7, "model": "glm-5.3-flash",
         "choices": [{"index": 0, "finish_reason": "tool_calls",
                      "message": {"role": "assistant", "content": "我来改",
                                  "tool_calls": [
                                      {"id": "call_f", "type": "function",
                                       "function": {"name": "read_file",
                                                    "arguments": "{\"path\": \"a.txt\"}"}},
                                      {"id": "call_p", "type": "function",
                                       "function": {"name": "apply_patch",
                                                    "arguments": "{\"input\": \"PATCH-TEXT\"}"}}]}}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                   "total_tokens": 7}}
rp = gateway._chat_to_response(comp2, "glm-5.3-flash", {"apply_patch"})
check("response 骨架", rp["object"] == "response" and rp["status"] == "completed"
      and rp["id"].startswith("resp_"), str(rp)[:120])
kinds = [o["type"] for o in rp["output"]]
check("output 条目顺序 message/function_call/custom_tool_call",
      kinds == ["message", "function_call", "custom_tool_call"], str(kinds))
check("custom_tool_call 的 input 已还原为原始字符串",
      rp["output"][2]["input"] == "PATCH-TEXT"
      and rp["output"][2]["call_id"] == "call_p", str(rp["output"][2]))
check("usage 映射", rp["usage"] == {"input_tokens": 3, "output_tokens": 4,
                                    "total_tokens": 7})

# ---------- 6. Responses 流式翻译 ----------
print("[6] Responses 流式事件序列")
raw = b"".join(gateway._responses_stream(
    iter([tool_sse()]), "glm-5.3-flash", set())).decode("utf-8")
events = re.findall(r"^event: (.+)$", raw, re.M)
check("以 response.created 开头、response.completed 收尾",
      events[0] == "response.created" and events[-1] == "response.completed",
      str(events))
check("含 function_call 参数增量事件",
      "response.function_call_arguments.delta" in events
      and "response.function_call_arguments.done" in events, str(events))
completed = [json.loads(l[6:]) for l in raw.splitlines()
             if l.startswith("data: ") and '"response.completed"' in l]
check("completed 里的 output 还原出 function_call",
      completed and completed[0]["response"]["output"]
      and completed[0]["response"]["output"][0]["type"] == "function_call"
      and completed[0]["response"]["output"][0]["arguments"]
      == "{\"city\": \"北京\"}", str(completed)[:200])
raw = b"".join(gateway._responses_stream(
    iter([text_sse("你好")]), "glm-5.3-flash", set())).decode("utf-8")
events = re.findall(r"^event: (.+)$", raw, re.M)
check("纯文本流含 output_text.delta",
      "response.output_text.delta" in events
      and "response.output_text.done" in events, str(events))

# ---------- 7. 出站画像断言（真实 _open_chat，mock urlopen） ----------
print("[7] 出站请求画像")
captures = []


def hdr(req, name):
    for k, v in req.headers.items():
        if k.lower() == name.lower():
            return v
    return None


orig_urlopen = urllib.request.urlopen
orig_refresh = cb_api.refresh_auth


def reset_breaker():
    gateway._breaker.update(fails=[], open_until=0.0, level=0)


reset_breaker()
behavior = {"fn": lambda req: FakeResp(text_sse("你好"))}


def fake_urlopen(req, timeout=None):
    captures.append(req)
    return behavior["fn"](req)


urllib.request.urlopen = fake_urlopen
store = FakeStore([make_rec("uprof", "画像", "tok-prof", "ref-prof")])
out = gateway.chat(store, {"model": "glm-5.3-flash", "stream": True,
                           "messages": [{"role": "user", "content": "你好"}]},
                   "sess-prof")
raw = b"".join(out.payload)
check("流式调用成功", out.status == 200 and "你好" in raw.decode("utf-8"))
req = captures[-1]
check("入口与路径", req.full_url ==
      "https://www.workbuddy.cn/v2/chat/completions", req.full_url)
check("UA 与官方一致", hdr(req, "User-Agent") ==
      "WorkBuddy/5.11.3 WorkBuddy/5.11.3 CLI/2.176.0", hdr(req, "User-Agent"))
for name, want in (
        ("X-Stainless-Package-Version", "6.25.0"),
        ("X-Stainless-Lang", "js"), ("X-Stainless-Runtime", "node"),
        ("X-Stainless-Runtime-Version", "v22.21.1"),
        ("X-Stainless-OS", "MacOS"), ("X-Stainless-Arch", "arm64"),
        ("X-Stainless-Retry-Count", "0"), ("X-Stainless-Timeout", "1200"),
        ("Accept", "application/json"), ("X-Private-Data", "true"),
        ("X-Client-Platform", "macos"), ("X-Client-Version", "5.11.3"),
        ("X-Client-Product", "WorkBuddy"), ("X-Agent-Runtime", "local-agent"),
        ("X-Agent-Intent", "craft"), ("X-Agent-Type", "main"),
        ("X-Requested-With", "XMLHttpRequest"), ("X-Product", "SaaS"),
        ("X-IDE-Name", "WorkBuddy"), ("X-IDE-Version", "5.11.3")):
    check(f"头 {name}={want}", hdr(req, name) == want,
          f"{hdr(req, name)}")
check("Accept-Encoding 含 gzip 且为官方集合",
      "gzip" in (hdr(req, "Accept-Encoding") or "")
      and "deflate" in (hdr(req, "Accept-Encoding") or ""),
      hdr(req, "Accept-Encoding"))
check("不发 Origin/Referer",
      hdr(req, "Origin") is None and hdr(req, "Referer") is None)
conv = hdr(req, "X-Conversation-ID") or ""
check("X-Conversation-ID 是带连字符 uuidv7",
      re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}"
                   r"-[0-9a-f]{12}", conv) is not None, conv)
rid = hdr(req, "X-Conversation-Request-ID") or ""
mid = hdr(req, "X-Conversation-Message-ID") or ""
check("请求/消息 id 是无连字符 uuidv7",
      re.fullmatch(r"[0-9a-f]{32}", rid) is not None and rid[12] == "7"
      and re.fullmatch(r"[0-9a-f]{32}", mid) is not None and mid[12] == "7",
      f"{rid} {mid}")
check("X-Request-ID == X-Conversation-Message-ID",
      hdr(req, "X-Request-ID") == mid)
tp = hdr(req, "traceparent") or ""
m = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01", tp)
check("traceparent 格式", m is not None, tp)
check("b3 与 traceparent 一致",
      m is not None and hdr(req, "b3") == f"{m.group(1)}-{m.group(2)}-1"
      and hdr(req, "X-B3-TraceId") == m.group(1)
      and hdr(req, "X-B3-SpanId") == m.group(2)
      and hdr(req, "X-B3-Sampled") == "1")
body1 = json.loads(req.data.decode("utf-8"))
check("body 强制 stream + cache_retention long",
      body1.get("stream") is True and body1.get("cache_retention") == "long")
hm = json.loads((body1.get("extra_vars") or {}).get("harnessMetadata") or "{}")
check("harnessMetadata 首步形状",
      hm.get("schemaVersion") == 1 and hm.get("harnessVersion") == "2.176.0"
      and hm.get("stepIndex") == 0 and "previous" not in hm
      and hm.get("agent") == {"depth": 0}, str(hm))
check("initiator=user（末条为 user 时）",
      (body1.get("extra_vars") or {}).get("initiator") == "user")

# ---------- 8. 同一轮工具续跑：req_id 复用 + harness 步进 ----------
print("[8] harness 步进链")
captures.clear()
behavior["fn"] = lambda req: FakeResp(tool_sse())
out = gateway.chat(store, {"model": "glm-5.3-flash",
                           "messages": [{"role": "user", "content": "天气？"}],
                           "tools": [{"type": "function", "function": {
                               "name": "get_weather",
                               "parameters": {"type": "object"}}}]},
                   "sess-turn")
out.payload and json.dumps(out.payload)  # 非流式聚合结果
req1 = captures[-1]
captures.clear()
out2 = gateway.chat(store, {"model": "glm-5.3-flash", "messages": [
    {"role": "user", "content": "天气？"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "get_weather", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "晴"}]},
    "sess-turn")
req2 = captures[-1]
check("续跑复用 X-Conversation-Request-ID",
      hdr(req2, "X-Conversation-Request-ID")
      == hdr(req1, "X-Conversation-Request-ID"))
check("续跑复用 X-Conversation-ID",
      hdr(req2, "X-Conversation-ID") == hdr(req1, "X-Conversation-ID"))
check("续跑换新消息 id",
      hdr(req2, "X-Conversation-Message-ID")
      != hdr(req1, "X-Conversation-Message-ID"))
hm2 = json.loads((json.loads(req2.data.decode("utf-8"))
                  .get("extra_vars") or {}).get("harnessMetadata") or "{}")
check("续跑 stepIndex=1 且 previous 链回上一消息 id",
      hm2.get("stepIndex") == 1
      and (hm2.get("previous") or {}).get("conversationMessageId")
      == hdr(req1, "X-Conversation-Message-ID"), str(hm2))
captures.clear()
gateway.chat(store, {"model": "glm-5.3-flash",
                     "messages": [{"role": "user", "content": "新话题"}]},
             "sess-turn")
req3 = captures[-1]
hm3 = json.loads((json.loads(req3.data.decode("utf-8"))
                  .get("extra_vars") or {}).get("harnessMetadata") or "{}")
check("新一轮 req_id 换新、stepIndex 归 0",
      hdr(req3, "X-Conversation-Request-ID")
      != hdr(req1, "X-Conversation-Request-ID")
      and hm3.get("stepIndex") == 0 and "previous" not in hm3)

# ---------- 9. 401 续期重试复用同一消息 id ----------
print("[9] 续期重试消息 id 复用")
reset_breaker()
captures.clear()
state = {"n": 0}


def flaky(req):
    state["n"] += 1
    if state["n"] == 1:
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", {},
            io.BytesIO(b'{"code": 12153, "msg": "expired"}'))
    return FakeResp(text_sse("续期后"))


behavior["fn"] = flaky
cb_api.refresh_auth = lambda auth: {
    "accessToken": "tok-new", "refreshToken": "ref-new",
    "expiresIn": 3600, "domain": "www.codebuddy.cn"}
out = gateway.chat(store, {"model": "glm-5.3-flash",
                           "messages": [{"role": "user", "content": "hi"}]},
                   None)
check("续期后成功", out.status == 200, str(out.status))
check("两次尝试消息 id 相同",
      len(captures) == 2
      and hdr(captures[0], "X-Conversation-Message-ID")
      == hdr(captures[1], "X-Conversation-Message-ID"),
      str([hdr(c, "X-Conversation-Message-ID") for c in captures]))
check("第二次带新令牌", hdr(captures[1], "Authorization") == "Bearer tok-new",
      hdr(captures[1], "Authorization"))
cb_api.refresh_auth = orig_refresh

# ---------- 10. 入口容灾 ----------
print("[10] 入口容灾与熔断")
reset_breaker()
captures.clear()
state2 = {"n": 0}


def failover_fn(req):
    state2["n"] += 1
    if "www.workbuddy.cn" in req.full_url:
        raise urllib.error.URLError("conn reset")
    return FakeResp(text_sse("备用"))


behavior["fn"] = failover_fn
out = gateway.chat(store, {"model": "glm-5.3-flash",
                           "messages": [{"role": "user", "content": "hi"}]},
                   None)
urls = [c.full_url for c in captures]
check("主入口传输错误改发备用入口",
      out.status == 200 and len(urls) == 2
      and "wb.tencentbuddy.com" in urls[1], str(urls))
check("换入口复用同一消息 id",
      hdr(captures[0], "X-Conversation-Message-ID")
      == hdr(captures[1], "X-Conversation-Message-ID"))
captures.clear()
reset_breaker()


def busy_fn(req):
    if "www.workbuddy.cn" in req.full_url:
        raise urllib.error.HTTPError(
            req.full_url, 503, "busy", {},
            io.BytesIO(b'{"code": 6001, "msg": "busy"}'))
    return FakeResp(text_sse("备用2"))


behavior["fn"] = busy_fn
out = gateway.chat(store, {"model": "glm-5.3-flash",
                           "messages": [{"role": "user", "content": "hi"}]},
                   None)
check("主入口 503 改发备用入口", out.status == 200 and len(captures) == 2,
      str([c.full_url for c in captures]))
captures.clear()
reset_breaker()


def no_retry_fn(req):
    raise urllib.error.HTTPError(
        req.full_url, 503, "busy", {},
        io.BytesIO(b'{"code": 6001, "msg": "busy", '
                   b'"retry_policy": {"retry": false}}'))


behavior["fn"] = no_retry_fn
out = gateway.chat(store, {"model": "glm-5.3-flash",
                           "messages": [{"role": "user", "content": "hi"}]},
                   None)
check("retry_policy.retry=false 时不换入口",
      out.status == 503 and len(captures) == 1, str(len(captures)))
# 熔断：连续 3 次主入口失败后直发备用
captures.clear()
reset_breaker()
behavior["fn"] = failover_fn
for _ in range(3):
    gateway.chat(store, {"model": "glm-5.3-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                 None)
captures.clear()
gateway.chat(store, {"model": "glm-5.3-flash",
                     "messages": [{"role": "user", "content": "hi"}]}, None)
check("熔断打开后直发备用入口",
      len(captures) == 1 and "wb.tencentbuddy.com" in captures[0].full_url,
      str([c.full_url for c in captures]))
reset_breaker()
urllib.request.urlopen = orig_urlopen

# ---------- 11. 设备身份持久化 ----------
print("[11] 设备身份")
ida = gateway._device_identity("acct-A")
idb = gateway._device_identity("acct-B")
ida2 = gateway._device_identity("acct-A")
check("同账号身份稳定复用", ida["machine_id"] == ida2["machine_id"]
      and ida["session_id"] == ida2["session_id"])
check("不同账号 machine_id 不同", ida["machine_id"] != idb["machine_id"])
check("machine_id 是大写 UUID 形",
      re.fullmatch(r"[0-9A-F-]{36}", ida["machine_id"]) is not None,
      ida["machine_id"])
check("设备基线是 mac 客户端",
      ida["stainless_os"] == "MacOS" and ida["stainless_arch"] == "arm64"
      and ida["client_platform"] == "macos")
dev_file = gateway.DATA_DIR / "gateway_devices.json"
check("设备身份文件落盘且权限 0600",
      dev_file.exists() and (os.stat(dev_file).st_mode & 0o777) == 0o600,
      oct(os.stat(dev_file).st_mode & 0o777) if dev_file.exists() else "缺失")

# ---------- 12. HTTP 层：新端点 + x-api-key ----------
print("[12] HTTP 端点")
key = gateway.ensure_key()


def open_ok(auth, uid, ent, payload, conv_id):
    if (payload.get("messages") or [{}])[-1].get("role") == "tool" or \
            any(m.get("tool_calls") for m in payload.get("messages") or []):
        return 200, FakeResp(tool_sse()), b""
    return 200, FakeResp(text_sse("你好")), b""


gateway._open_chat = open_ok
http_store = FakeStore([make_rec("uH2", "H2", "tok-H2", "ref-H2")])
server.AccountStore = lambda: http_store
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


st_code, data = req("POST", "/v1/messages", body=json.dumps({
    "model": "glm-5.3-flash", "max_tokens": 100,
    "messages": [{"role": "user", "content": "你好"}]}).encode(),
    headers={"Content-Type": "application/json"})
obj = json.loads(data)
check("无密钥 /v1/messages → 401 Anthropic 形状",
      st_code == 401 and obj.get("type") == "error"
      and obj["error"]["type"] == "authentication_error",
      f"{st_code} {data[:140]}")
st_code, data = req("POST", "/v1/messages", body=json.dumps({
    "model": "glm-5.3-flash", "max_tokens": 100,
    "messages": [{"role": "user", "content": "你好"}]}).encode(),
    headers={"Content-Type": "application/json", "x-api-key": key})
obj = json.loads(data)
check("x-api-key /v1/messages → 200 Anthropic 回复",
      st_code == 200 and obj.get("type") == "message"
      and obj["content"][0]["text"] == "你好"
      and obj["stop_reason"] == "end_turn", f"{st_code} {data[:200]}")
st_code, data = req("POST", "/v1/messages", body=json.dumps({
    "model": "glm-5.3-flash", "max_tokens": 100, "stream": True,
    "messages": [{"role": "user", "content": "你好"}]}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + key})
text = data.decode("utf-8")
check("Bearer 流式 /v1/messages → Anthropic 事件流",
      st_code == 200 and "event: message_start" in text
      and "event: message_stop" in text and "text_delta" in text,
      f"{st_code} {text[:160]}")
st_code, data = req("POST", "/v1/responses", body=json.dumps({
    "model": "glm-5.3-flash", "input": "你好"}).encode(),
    headers={"Content-Type": "application/json", "x-api-key": key})
obj = json.loads(data)
check("x-api-key /v1/responses → 200 Responses 回复",
      st_code == 200 and obj.get("object") == "response"
      and obj["output"][0]["content"][0]["text"] == "你好",
      f"{st_code} {data[:200]}")
st_code, data = req("POST", "/v1/responses", body=json.dumps({
    "model": "glm-5.3-flash", "input": "你好", "stream": True}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + key})
text = data.decode("utf-8")
check("Bearer 流式 /v1/responses → response.* 事件流",
      st_code == 200 and "response.created" in text
      and "response.completed" in text, f"{st_code} {text[:160]}")
st_code, _ = req("POST", "/v1/responses", body=json.dumps({
    "model": "glm-5.3-flash", "input": "你好"}).encode(),
    headers={"Content-Type": "application/json"})
check("无密钥 /v1/responses → 401", st_code == 401)
st_code, _ = req("POST", "/v1/chat/completions", body=json.dumps({
    "model": "glm-5.3-flash",
    "messages": [{"role": "user", "content": "你好"}]}).encode(),
    headers={"Content-Type": "application/json", "x-api-key": key})
check("一期端点仍只认 Bearer（x-api-key 不放行）", st_code == 401)
httpd.shutdown()

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
