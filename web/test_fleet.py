# -*- coding: utf-8 -*-
"""第三方设备接入（fleet）夹具测试：临时 APPDATA + 预置假接入密钥，
真实起 HTTP 服务走全链路（鉴权/心跳 upsert/在线判定/短信入库/未知
imei 自动建设备/展示页登录与转义）。运行：python3 test_fleet.py"""
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="cbam-fleet-test-")
os.environ["APPDATA"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA_DIR = Path(TMP) / "CodeBuddyAccountManager"
DATA_DIR.mkdir(parents=True, exist_ok=True)

FAKE_KEY = "sk-fleet-testkey0123456789abcdefXYZ"
(DATA_DIR / "fleet.json").write_text(json.dumps({
    "key_plain": FAKE_KEY,
    "key_hash": hashlib.sha256(FAKE_KEY.encode("utf-8")).hexdigest(),
    "created_at": "2026-11-27 00:00:00",
}, ensure_ascii=False, indent=2), encoding="utf-8")

import fleet  # noqa: E402
import server  # noqa: E402

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


check("假密钥已就位", fleet.key_plain() == FAKE_KEY)
check("掩码样式与网关卡一致",
      fleet.key_masked() == FAKE_KEY[:10] + "••••••" + FAKE_KEY[-4:],
      fleet.key_masked())

conf = server.load_conf()
server.set_password(conf, "test-pass-123")
server.save_conf(conf)

srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


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


KEY_H = {"X-Device-Key": FAKE_KEY}
IMEI = "860000000000001"

# ---------- 鉴权 ----------
st, _, txt = req("POST", "/api/v1/heartbeat", {"imei": IMEI})
check("心跳无密钥 401", st == 401, f"{st} {txt[:80]}")
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI},
               {"X-Device-Key": "sk-fleet-wrong"})
check("心跳错密钥 401", st == 401)
st, _, _ = req("POST", "/api/v1/sms", {"imei": IMEI, "content": "x"})
check("短信无密钥 401", st == 401)

# ---------- 心跳 upsert ----------
st, _, txt = req("POST", "/api/v1/heartbeat",
                 {"imei": IMEI, "phone": "13800000000", "online": True,
                  "model": "Pixel 8", "battery": 85, "app_version": "1.2.3"},
                 KEY_H)
check("心跳 200 ok", st == 200 and json.loads(txt).get("ok") is True,
      f"{st} {txt[:80]}")
devs = {d["imei"]: d for d in fleet.list_devices()}
d = devs.get(IMEI) or {}
check("心跳建设备", bool(d))
check("手机号落盘", d.get("phone") == "13800000000", str(d.get("phone")))
check("刚上报判在线", d.get("online_now") is True)
check("型号/电量落盘", d.get("model") == "Pixel 8" and d.get("battery") == "85",
      f"{d.get('model')} {d.get('battery')}")
seen1 = d.get("last_seen") or 0

time.sleep(1.1)
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI, "online": False},
               KEY_H)
devs = {x["imei"]: x for x in fleet.list_devices()}
d = devs[IMEI]
check("二次心跳更新 last_seen", (d.get("last_seen") or 0) > seen1)
check("未重报的手机号保留", d.get("phone") == "13800000000")
check("online=false 显示离线", d.get("online_now") is False)

# 心跳超时也判离线：把 last_seen 拨回 1000 秒前且 online_reported=1
conn = sqlite3.connect(str(fleet.DB_FILE))
conn.execute("UPDATE devices SET online_reported=1, last_seen=? WHERE imei=?",
             (time.time() - 1000, IMEI))
conn.commit()
conn.close()
devs = {x["imei"]: x for x in fleet.list_devices()}
check("心跳超时判离线", devs[IMEI].get("online_now") is False)

# Bearer 也认
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI, "online": True},
               {"Authorization": "Bearer " + FAKE_KEY})
check("Bearer 鉴权可用", st == 200)

# 缺 imei → 400
st, _, _ = req("POST", "/api/v1/heartbeat", {"phone": "1"}, KEY_H)
check("心跳缺 imei 400", st == 400)

# ---------- 短信上报 ----------
st, _, txt = req("POST", "/api/v1/sms",
                 {"imei": IMEI, "from": "10086",
                  "content": "您的验证码是 123456", "ts": 1764000000000},
                 KEY_H)
body = json.loads(txt) if st == 200 else {}
check("短信 200 带 id", st == 200 and body.get("ok") is True
      and isinstance(body.get("id"), int), f"{st} {txt[:80]}")
msgs = fleet.list_sms()
m = msgs[0] if msgs else {}
check("短信入库", m.get("content") == "您的验证码是 123456"
      and m.get("sender") == "10086", str(m)[:120])
check("短信关联出手机号", m.get("phone") == "13800000000")
check("设备毫秒时间戳已换算", abs((m.get("device_ts") or 0) - 1764000000) < 1,
      str(m.get("device_ts")))

# 未知 imei：自动建设备
st, _, txt = req("POST", "/api/v1/sms",
                 {"imei": "860000000000099", "content": "未知设备短信"}, KEY_H)
check("未知 imei 短信也收", st == 200 and json.loads(txt).get("ok") is True)
devs = {x["imei"]: x for x in fleet.list_devices()}
check("未知 imei 自动建设备", "860000000000099" in devs
      and not devs["860000000000099"].get("phone"))

# 缺 content → 400；坏 JSON → 400
st, _, _ = req("POST", "/api/v1/sms", {"imei": IMEI}, KEY_H)
check("短信缺 content 400", st == 400)
st, _, _ = req("POST", "/api/v1/sms", headers=KEY_H, raw=b"{not json")
check("短信坏 JSON 400", st == 400)

# 带脚本的短信内容（展示页必须转义）
req("POST", "/api/v1/sms",
    {"imei": IMEI, "content": "<script>alert(1)</script>"}, KEY_H)

# ---------- 展示页 ----------
st, hdrs, _ = req("GET", "/fleet")
check("/fleet 未登录 303 跳登录",
      st == 303 and "/login" in (hdrs.get("Location") or ""),
      f"{st} {hdrs.get('Location')}")

st, hdrs, _ = req("POST", "/login", raw=b"password=test-pass-123",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
cookie = hdrs.get("Set-Cookie") or ""
check("登录拿到会话 cookie", st == 303 and "cbw_session=" in cookie,
      f"{st} {cookie[:40]}")
sess = cookie.split(";")[0]
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("/fleet 登录后 200", st == 200, str(st))
check("页含接口完整地址", "/api/v1/heartbeat" in htmltxt
      and "/api/v1/sms" in htmltxt)
check("页含掩码密钥与明文", fleet.key_masked() in htmltxt
      and FAKE_KEY in htmltxt)
check("页含 curl 示例", "curl -X POST" in htmltxt)
check("页含 30 秒自动刷新", "http-equiv='refresh' content='30'" in htmltxt)
check("设备行渲染", IMEI in htmltxt and "13800000000" in htmltxt
      and "Pixel 8" in htmltxt and "85%" in htmltxt)
check("短信行渲染", "您的验证码是 123456" in htmltxt and "10086" in htmltxt)
check("短信内容被转义", "&lt;script&gt;alert(1)" in htmltxt
      and "<script>alert(1)</script>" not in htmltxt)

# ---------- 设备类型（心跳上报） ----------
import urllib.parse  # noqa: E402


def form_post(path, data, cookie):
    raw = urllib.parse.urlencode(data).encode("utf-8")
    return req("POST", path, raw=raw, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie})


st, _, _ = req("POST", "/api/v1/heartbeat",
               {"imei": IMEI, "online": True, "type": "手机"}, KEY_H)
check("心跳带 type 200", st == 200)
devs = {x["imei"]: x for x in fleet.list_devices()}
check("心跳 type 入库", devs[IMEI].get("type") == "手机",
      str(devs[IMEI].get("type")))
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("设备表有类型/备注列", ">类型</a>" in htmltxt
      and "<th>备注</th>" in htmltxt)
check("类型值渲染", "<td>手机</td>" in htmltxt)

# ---------- 设备编辑（登录态 POST） ----------
st, hdrs, _ = form_post("/fleet/device/update",
                        {"imei": IMEI, "phone": "13911112222",
                         "type": "平板", "remark": "测试备注甲"}, sess)
check("编辑设备 303 跳回", st == 303
      and "/fleet" in (hdrs.get("Location") or ""), f"{st}")
devs = {x["imei"]: x for x in fleet.list_devices()}
d = devs[IMEI]
check("编辑落盘 phone/type/remark",
      d.get("phone") == "13911112222" and d.get("type") == "平板"
      and d.get("remark") == "测试备注甲", str(d)[:160])
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("列表渲染编辑后新值", "13911112222" in htmltxt and "平板" in htmltxt
      and "测试备注甲" in htmltxt)
check("行内编辑表单存在", "fleetToggleEdit" in htmltxt
      and "/fleet/device/update" in htmltxt)
st, hdrs, _ = form_post("/fleet/device/update",
                        {"imei": "000000000000000", "phone": "1"}, sess)
check("编辑不存在的设备回 err", "err=1" in (hdrs.get("Location") or ""),
      hdrs.get("Location") or "")

# ---------- 短信 1 小时自动清理 ----------
st, _, _ = req("POST", "/api/v1/sms",
               {"imei": IMEI, "content": "过期短信XYZ"}, KEY_H)
check("待过期短信先入库", st == 200)
conn = sqlite3.connect(str(fleet.DB_FILE))
conn.execute("UPDATE sms SET received_at=? WHERE content='过期短信XYZ'",
             (time.time() - 3700,))
conn.commit()
conn.close()
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("超 1 小时短信读页面时被清", "过期短信XYZ" not in htmltxt)
check("过期短信已从库清除",
      all(m["content"] != "过期短信XYZ" for m in fleet.list_sms()))
check("页面写明 1 小时规则", "短信保留 1 小时" in htmltxt)
# 入库顺带清：直插一条老短信，再经 API 入一条新的，老的应被清掉
conn = sqlite3.connect(str(fleet.DB_FILE))
old_ts = time.time() - 3700
conn.execute("INSERT INTO sms(imei, sender, content, device_ts, received_at)"
             " VALUES(?,?,?,?,?)", (IMEI, "10086", "老短信入库清",
                                    old_ts, old_ts))
conn.commit()
conn.close()
req("POST", "/api/v1/sms", {"imei": IMEI, "content": "新短信触发清"}, KEY_H)
check("新短信入库顺带清过期",
      all(m["content"] != "老短信入库清" for m in fleet.list_sms()))

# ---------- 短信手动删 / 清空 ----------
msgs = fleet.list_sms()
check("删前有短信", len(msgs) > 0)
target = msgs[0]
st, _, _ = form_post("/fleet/sms/delete", {"id": target["id"]}, sess)
check("手动删短信 303", st == 303, str(st))
check("被删短信不在库", all(m["id"] != target["id"]
                            for m in fleet.list_sms()))
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("清空按钮存在", "清空全部短信" in htmltxt
      and "/fleet/sms/clear" in htmltxt)
st, _, _ = form_post("/fleet/sms/clear", {}, sess)
check("清空短信 303", st == 303, str(st))
check("清空后库为空", fleet.list_sms() == [])

# ---------- 双分页 + 筛选 + 复制/看短信 ----------
for n in range(23):  # 加上已有 2 台 → 共 25 台设备
    fleet.heartbeat({"imei": f"860000000001{n:03d}",
                     "phone": f"137{n:08d}", "online": True})
for n in range(30):
    fleet.record_sms({"imei": IMEI, "from": "10086",
                      "content": f"分页短信-A-{n:02d}"})
for n in range(15):
    fleet.record_sms({"imei": "860000000000099", "from": "10010",
                      "content": f"分页短信-B-{n:02d}"})
fleet.record_sms({"imei": IMEI, "from": "95588", "content": "银行动账提醒"})
_, dtotal = fleet.list_devices_page(1)
_, stotal_all = fleet.list_sms_page(1)
check("设备总数 25", dtotal == 25, str(dtotal))
check("短信总数 46", stotal_all == 46, str(stotal_all))

st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("设备第 1/2 页", "第 1/2 页" in htmltxt)
check("设备第一页 20 行编辑区", htmltxt.count("id='fleet-edit-") == 20,
      str(htmltxt.count("id='fleet-edit-")))
check("短信第 1/3 页", "第 1/3 页" in htmltxt)
check("短信第一页 20 条复制钮", htmltxt.count("smsCopy(") >= 20,
      str(htmltxt.count("smsCopy(")))
check("短信复制按钮存在", ">复制</button>" in htmltxt)
check("看短信链接预筛设备", "看短信" in htmltxt and "#sms" in htmltxt
      and "sdev=8600" in htmltxt)
check("设备筛选下拉存在", "<select name='sdev'" in htmltxt
      and "全部设备" in htmltxt)

st, _, htmltxt = req("GET", "/fleet?spage=3", headers={"Cookie": sess})
check("短信翻到第 3/3 页", "第 3/3 页" in htmltxt)
check("短信第三页 6 条", htmltxt.count("smsCopy(") - 1 == 6,
      str(htmltxt.count("smsCopy(")))
check("翻短信页不影响设备页", "第 1/2 页" in htmltxt)
check("带参页仍有自动刷新", "http-equiv='refresh' content='30'" in htmltxt)

st, _, htmltxt = req("GET", "/fleet?dpage=2", headers={"Cookie": sess})
check("设备翻到第 2/2 页", "第 2/2 页" in htmltxt)
check("设备第二页 5 行", htmltxt.count("id='fleet-edit-") == 5,
      str(htmltxt.count("id='fleet-edit-")))
check("翻设备页不影响短信页", "第 1/3 页" in htmltxt)
check("第二页有本机看短信链接", f"sdev={IMEI}" in htmltxt)

st, _, htmltxt = req("GET", "/fleet?dpage=2&spage=3",
                     headers={"Cookie": sess})
check("双参数同时生效", "第 2/2 页" in htmltxt and "第 3/3 页" in htmltxt)

st, _, htmltxt = req("GET", f"/fleet?sdev={IMEI}",
                     headers={"Cookie": sess})
check("按设备筛选短信 31 条分 2 页", "第 1/2 页 · 共 31 条短信" in htmltxt)
check("筛选后只有 A 设备短信", "分页短信-A-" in htmltxt
      and "分页短信-B-" not in htmltxt)

st, _, htmltxt = req("GET", "/fleet?sq=" + urllib.parse.quote("分页短信-B"),
                     headers={"Cookie": sess})
check("关键词搜内容", "分页短信-B-" in htmltxt
      and "分页短信-A-" not in htmltxt and "共 15 条短信" in htmltxt)
st, _, htmltxt = req("GET", "/fleet?sq=95588", headers={"Cookie": sess})
check("关键词搜发送方", "银行动账提醒" in htmltxt
      and "共 1 条短信" in htmltxt)

# ---------- 在线时长 ----------
check("时长格式化口径",
      fleet.fmt_duration(30) == "刚刚"
      and fleet.fmt_duration(25 * 60) == "25分钟"
      and fleet.fmt_duration(2 * 3600 + 15 * 60) == "2小时15分"
      and fleet.fmt_duration(3 * 86400 + 4 * 3600) == "3天4小时",
      fleet.fmt_duration(2 * 3600 + 15 * 60))
IMEI2 = "860000000000777"
t0 = time.time()
st, _, _ = req("POST", "/api/v1/heartbeat",
               {"imei": IMEI2, "online": True, "phone": "13600000000"}, KEY_H)
check("在线时长设备心跳 200", st == 200)
devs = {x["imei"]: x for x in fleet.list_devices()}
d2 = devs.get(IMEI2) or {}
check("首次上线打 online_since",
      d2.get("online_since") is not None
      and abs(d2["online_since"] - t0) < 5, str(d2.get("online_since")))
check("刚上线时长显示刚刚", d2.get("online_duration") == "刚刚",
      str(d2.get("online_duration")))
since1 = d2["online_since"]
time.sleep(1.1)
req("POST", "/api/v1/heartbeat", {"imei": IMEI2, "online": True}, KEY_H)
devs = {x["imei"]: x for x in fleet.list_devices()}
check("持续在线不重置 online_since",
      devs[IMEI2].get("online_since") == since1,
      f"{since1} -> {devs[IMEI2].get('online_since')}")
req("POST", "/api/v1/heartbeat", {"imei": IMEI2, "online": False}, KEY_H)
devs = {x["imei"]: x for x in fleet.list_devices()}
d2 = devs[IMEI2]
check("报离线清空 online_since", d2.get("online_since") is None)
check("离线设备时长显示 —", d2.get("online_duration") == "—",
      str(d2.get("online_duration")))
req("POST", "/api/v1/heartbeat", {"imei": IMEI2, "online": True}, KEY_H)
devs = {x["imei"]: x for x in fleet.list_devices()}
since2 = devs[IMEI2].get("online_since")
check("离线后重新上线重新打点", since2 is not None and since2 > since1)
conn = sqlite3.connect(str(fleet.DB_FILE))
conn.execute("UPDATE devices SET last_seen=?, online_since=? WHERE imei=?",
             (time.time() - 1000, time.time() - 1000, IMEI2))
conn.commit()
conn.close()
t1 = time.time()
req("POST", "/api/v1/heartbeat", {"imei": IMEI2, "online": True}, KEY_H)
devs = {x["imei"]: x for x in fleet.list_devices()}
since3 = devs[IMEI2].get("online_since")
check("心跳超时后再上线重新计时",
      since3 is not None and since3 > since2 and abs(since3 - t1) < 5,
      f"{since2} -> {since3}")
conn = sqlite3.connect(str(fleet.DB_FILE))
conn.execute("UPDATE devices SET online_since=? WHERE imei=?",
             (time.time() - (2 * 3600 + 15 * 60), IMEI2))
conn.commit()
conn.close()
devs = {x["imei"]: x for x in fleet.list_devices()}
check("在线时长人话显示", devs[IMEI2].get("online_duration") == "2小时15分",
      str(devs[IMEI2].get("online_duration")))
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("设备表有在线时长列", ">在线时长</a>" in htmltxt)
check("页面渲染在线时长", "2小时15分" in htmltxt)

# ---------- 短信双时间列（上传时间 / 短信时间） ----------
past_ts_ms = int((time.time() - 7200) * 1000)
st, _, _ = req("POST", "/api/v1/sms",
               {"imei": IMEI, "from": "10086", "content": "双时间列校验短信",
                "ts": past_ts_ms}, KEY_H)
check("双时间短信入库", st == 200)
rec = next((m for m in fleet.list_sms()
            if m["content"] == "双时间列校验短信"), None)
check("双时间记录存在", rec is not None)
up_s = fleet.fmt_ts(rec["received_at"]) if rec else ""
dev_s = fleet.fmt_ts(rec["device_ts"]) if rec else ""
check("上传/短信时间确实不同", bool(rec) and up_s != dev_s,
      f"{up_s} vs {dev_s}")
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("短信表有双时间列头", ">上传时间 ▼</a>" in htmltxt
      and ">短信时间</a>" in htmltxt)
pos = htmltxt.find("双时间列校验短信")
seg = htmltxt[max(0, pos - 800):pos] if pos >= 0 else ""
check("行内上传时间在前、短信时间在后",
      bool(seg) and 0 <= seg.find(up_s) < seg.find(dev_s),
      f"up@{seg.find(up_s)} dev@{seg.find(dev_s)}")

# ---------- 表头排序（服务端） ----------
S1, S2, S3 = "860000000000901", "860000000000902", "860000000000903"
base_t = time.time()
fleet.heartbeat({"imei": S1, "phone": "13711111111", "type": "平板",
                 "online": True})
fleet.heartbeat({"imei": S2, "phone": "13722222222", "type": "手机",
                 "online": True})
fleet.heartbeat({"imei": S3, "phone": "13733333333", "type": "网关",
                 "online": False})
conn = sqlite3.connect(str(fleet.DB_FILE))
conn.execute("UPDATE devices SET last_seen=?, online_since=?"
             " WHERE imei=?", (base_t - 10, base_t - 3600, S1))
conn.execute("UPDATE devices SET last_seen=?, online_since=?"
             " WHERE imei=?", (base_t - 20, base_t - 60, S2))
conn.execute("UPDATE devices SET last_seen=?, online_since=NULL"
             " WHERE imei=?", (base_t - 30, S3))
conn.commit()
conn.close()


def dev_order(sort, order):
    rows, _ = fleet.list_devices_page(1, 100, sort=sort, order=order)
    return [r["imei"] for r in rows]


def rel(seq, *items):
    idx = [seq.index(i) for i in items]
    return idx == sorted(idx)


check("设备按手机号升序", rel(dev_order("phone", "asc"), S1, S2, S3))
check("设备按手机号降序", rel(dev_order("phone", "desc"), S3, S2, S1))
check("设备按类型升序", rel(dev_order("type", "asc"), S1, S2, S3))
_od = dev_order("status", "desc")
check("设备按状态降序在线在前", rel(_od, S1, S3) and rel(_od, S2, S3))
_oa = dev_order("status", "asc")
check("设备按状态升序离线在前", rel(_oa, S3, S1) and rel(_oa, S3, S2))
check("设备默认最后心跳降序", rel(dev_order("last_seen", "desc"),
                                  S1, S2, S3))
check("设备最后心跳升序", rel(dev_order("last_seen", "asc"), S3, S2, S1))
check("在线时长降序长者在前离线沉底",
      rel(dev_order("duration", "desc"), S1, S2, S3))
_oa = dev_order("duration", "asc")
check("在线时长升序短者在前离线仍沉底",
      rel(_oa, S2, S1) and _oa.index(S3) > _oa.index(S1))

fleet.record_sms({"imei": S1, "from": "10086", "content": "排序短信甲"})
fleet.record_sms({"imei": S2, "from": "10010", "content": "排序短信乙"})
fleet.record_sms({"imei": IMEI, "from": "95588", "content": "排序短信丙"})
conn = sqlite3.connect(str(fleet.DB_FILE))
for content, recv, dts in (("排序短信甲", base_t - 100, base_t - 500),
                           ("排序短信乙", base_t - 50, base_t - 900),
                           ("排序短信丙", base_t - 10, base_t - 100)):
    conn.execute("UPDATE sms SET received_at=?, device_ts=?"
                 " WHERE content=?", (recv, dts, content))
conn.commit()
conn.close()


def sms_order(sort, order):
    rows, _ = fleet.list_sms_page(1, 100, keyword="排序短信",
                                  sort=sort, order=order)
    return [r["content"] for r in rows]


check("短信默认上传时间降序",
      sms_order("received", "desc")
      == ["排序短信丙", "排序短信乙", "排序短信甲"],
      str(sms_order("received", "desc")))
check("短信上传时间升序",
      sms_order("received", "asc")
      == ["排序短信甲", "排序短信乙", "排序短信丙"])
check("短信按短信时间降序",
      sms_order("device_ts", "desc")
      == ["排序短信丙", "排序短信甲", "排序短信乙"],
      str(sms_order("device_ts", "desc")))
check("短信按发送方升序",
      sms_order("sender", "asc")
      == ["排序短信乙", "排序短信甲", "排序短信丙"],
      str(sms_order("sender", "asc")))
check("短信按设备升序",
      sms_order("device", "asc")
      == ["排序短信甲", "排序短信乙", "排序短信丙"],
      str(sms_order("device", "asc")))

st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("默认设备表头最后心跳带 ▼", ">最后心跳 ▼</a>" in htmltxt)
check("可排序表头是链接", "dsort=phone" in htmltxt
      and "ssort=sender" in htmltxt)
st, _, htmltxt = req("GET", "/fleet?dsort=phone&dorder=asc",
                     headers={"Cookie": sess})
check("手机号当前列带 ▲", ">手机号 ▲</a>" in htmltxt)
_i = htmltxt.find(">手机号 ▲</a>")
_href_seg = htmltxt[max(0, _i - 80):_i] if _i >= 0 else ""
check("点当前列切换为降序（链接不再带升序）",
      "dsort=phone" in _href_seg and "dorder=asc" not in _href_seg,
      _href_seg)
check("页面按手机号升序排列",
      htmltxt.find("13711111111") < htmltxt.find("13722222222")
      < htmltxt.find("13733333333"))
st, _, htmltxt = req("GET", "/fleet?dsort=phone&dorder=asc&dpage=2",
                     headers={"Cookie": sess})
check("翻页后排序保持", ">手机号 ▲</a>" in htmltxt
      and "dsort=phone" in htmltxt)
st, _, htmltxt = req("GET", "/fleet?ssort=sender&sorder=asc&sq="
                     + urllib.parse.quote("排序短信"),
                     headers={"Cookie": sess})
check("发送方当前列带 ▲", ">发送方 ▲</a>" in htmltxt)
check("筛选下按发送方升序排列",
      htmltxt.find("排序短信乙") < htmltxt.find("排序短信甲")
      < htmltxt.find("排序短信丙"))
st, _, htmltxt = req("GET", "/fleet?ssort=sender&sorder=asc&spage=2",
                     headers={"Cookie": sess})
check("短信翻页后排序保持", ">发送方 ▲</a>" in htmltxt)

# ---------- 改接入密钥：自定义 ----------
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("改密钥表单存在", "/fleet/key/update" in htmltxt
      and "/fleet/key/rotate" in htmltxt and "保存新密钥" in htmltxt
      and "随机换一把" in htmltxt
      and "旧密钥立即失效" in htmltxt)
NEW_KEY = "my-custom-key_2026!"
st, hdrs, _ = form_post("/fleet/key/update", {"new_key": NEW_KEY}, sess)
check("自定义改密钥 303 跳回", st == 303
      and "/fleet" in (hdrs.get("Location") or ""), f"{st}")
check("改后当前密钥是新值", fleet.key_plain() == NEW_KEY)
conf_file = json.loads((DATA_DIR / "fleet.json").read_text(encoding="utf-8"))
check("fleet.json 明文与哈希同步",
      conf_file.get("key_plain") == NEW_KEY
      and conf_file.get("key_hash")
      == hashlib.sha256(NEW_KEY.encode("utf-8")).hexdigest())
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI}, KEY_H)
check("改密钥后旧密钥心跳 401", st == 401)
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI},
               {"X-Device-Key": NEW_KEY})
check("新密钥心跳 200", st == 200)
st, _, _ = req("POST", "/api/v1/sms", {"imei": IMEI, "content": "新密钥短信"},
               {"Authorization": "Bearer " + NEW_KEY})
check("新密钥 Bearer 短信 200", st == 200)
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("页面显示新密钥明文", NEW_KEY in htmltxt)

# ---------- 改接入密钥：不合格被拒 ----------
for bad, why in (("short", "太短"), ("has space99", "带空格"),
                 ("含中文的密钥abcd1234", "非 ASCII"),
                 ("with\"quote99", "带引号")):
    st, hdrs, _ = form_post("/fleet/key/update", {"new_key": bad}, sess)
    check(f"不合格密钥被拒（{why}）", "err=1" in (hdrs.get("Location") or ""),
          hdrs.get("Location") or "")
check("被拒后密钥未变", fleet.key_plain() == NEW_KEY)
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI},
               {"X-Device-Key": NEW_KEY})
check("被拒后旧密钥仍可用", st == 200)

# ---------- 改接入密钥：随机更换 ----------
st, hdrs, _ = form_post("/fleet/key/rotate", {}, sess)
check("随机更换 303 跳回", st == 303
      and "/fleet" in (hdrs.get("Location") or ""), f"{st}")
rotated = fleet.key_plain()
check("随机新密钥 sk-fleet- 前缀且已换",
      rotated.startswith("sk-fleet-") and rotated != NEW_KEY,
      rotated[:12])
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI},
               {"X-Device-Key": NEW_KEY})
check("随机更换后旧密钥 401", st == 401)
st, _, _ = req("POST", "/api/v1/heartbeat", {"imei": IMEI},
               {"X-Device-Key": rotated})
check("随机更换后新密钥 200", st == 200)
st, _, htmltxt = req("GET", "/fleet", headers={"Cookie": sess})
check("页面显示随机新密钥", rotated in htmltxt)

# ---------- 改接入密钥：未登录被挡 ----------
st, hdrs, _ = form_post("/fleet/key/update",
                        {"new_key": "another-key-123"}, "")
check("未登录改密钥跳登录", st == 303
      and "/login" in (hdrs.get("Location") or ""),
      f"{st} {hdrs.get('Location')}")
st, hdrs, _ = form_post("/fleet/key/rotate", {}, "")
check("未登录随机更换跳登录", st == 303
      and "/login" in (hdrs.get("Location") or ""))
check("未登录尝试未改掉密钥", fleet.key_plain() == rotated)

srv.shutdown()
print(f"\n== fleet: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
