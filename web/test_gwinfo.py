# -*- coding: utf-8 -*-
"""设置页网关参考区夹具测试：用从生产只读拉回的真实官方目录样本
（catalog_sample.json，仅 data.models，无任何密钥）走全链路：
_fetch_catalog_raw 打桩 → catalog_display 行值逐字核对 → 设置页渲染断言。
运行：python3 test_gwinfo.py"""
import copy
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="cbam-gwinfo-test-")
os.environ["APPDATA"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gateway  # noqa: E402
import server  # noqa: E402
from account_store import _strip_uid_prefix, build_ide_session  # noqa: E402


class FakeStore:
    """与 test_gateway.py 同款假账号库（本机 python 缺 cryptography，
    真 AccountStore 的加密落盘跑不了；目录链路本身全程走真代码）。"""

    def __init__(self, recs):
        self.recs = {r["id"]: r for r in recs}

    def list_accounts(self):
        return list(self.recs.values())

    def get(self, rid):
        return self.recs.get(rid)

    def update(self, rec):
        self.recs[rec["id"]] = rec
        return True

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


def make_rec(uid, label, access, refresh):
    plain = build_ide_session({
        "account": {"uid": uid, "label": label, "domain": "www.codebuddy.cn"},
        "auth": {"accessToken": _strip_uid_prefix(access),
                 "refreshToken": refresh, "domain": "www.codebuddy.cn"}})
    return {"id": uid, "uid": uid, "label": label,
            "keys": {"##current##": plain}, "in_pool": True,
            "quota": {"usage_left": 100.0}}


SAMPLE = json.loads(open(os.path.join(HERE, "catalog_sample.json"),
                         encoding="utf-8").read())
check("样本里没有密钥类字符串",
      "sk-cbam" not in json.dumps(SAMPLE) and "accessToken" not in json.dumps(SAMPLE))

store = FakeStore([make_rec("u-info", "参考区测试号", "tok-info", "ref-info")])
# 设置页内部用的是 server 模块名下的 AccountStore，换成同一个假库
server.AccountStore = lambda *a, **k: store

orig_raw = gateway._fetch_catalog_raw
gateway._fetch_catalog_raw = lambda auth, uid: copy.deepcopy(SAMPLE)


def reset_cache():
    gateway._models_cache.clear()
    gateway._models_cache.update(at=0, data=None)


# ---------- 1. catalog_display 行值 ----------
print("[1] catalog_display 与样本逐字一致")
reset_cache()
rows = gateway.catalog_display(store)
check("目录行数 = 样本条数", rows is not None and len(rows) == len(SAMPLE),
      f"{None if rows is None else len(rows)} vs {len(SAMPLE)}")
by_id = {r["id"]: r for r in rows or []}
g = by_id.get("glm-5.3-flash") or {}
check("glm-5.3-flash 上下文原样",
      g.get("context") == "300000（可选 300000 / 600000 / 1000000）",
      str(g.get("context")))
check("glm-5.3-flash 最大输出原值", g.get("max_output") == 131072,
      str(g.get("max_output")))
check("glm-5.3-flash 积分倍率原值", g.get("credits") == "x0.08",
      str(g.get("credits")))
a = by_id.get("auto") or {}
check("auto 上下文 = maxInputTokens 原值", a.get("context") == "256000",
      str(a.get("context")))
check("auto 最大输出原值", a.get("max_output") == 32000, str(a.get("max_output")))
check("auto 无 credits 字段", a.get("credits") is None, str(a.get("credits")))
k = by_id.get("kling-video-v3") or {}
check("视频模型照样列出", bool(k))
check("视频模型 tags 原样", k.get("tags") == ["text-to-video", "image-to-video"],
      str(k.get("tags")))
check("视频模型上下文未给", k.get("context") is None, str(k.get("context")))
h = by_id.get("hunyuan-image-alpha") or {}
check("图像模型照样列出且无积分字段", bool(h) and h.get("credits") is None)

# ---------- 2. /v1/models 裁剪口径未被改动 ----------
print("[2] list_models 同源且裁剪口径不变")
reset_cache()
data = gateway.list_models(store)
tm = {m["id"]: m for m in data["data"]}
check("裁剪版 glm-5.3-flash context_length 仍是目录原 dict",
      (tm.get("glm-5.3-flash") or {}).get("context_length") ==
      {"defaultLength": 300000, "supportedLengths": [300000, 600000, 1000000]},
      str((tm.get("glm-5.3-flash") or {}).get("context_length")))
check("裁剪版带 credits 原值",
      (tm.get("glm-5.3-flash") or {}).get("credits") == "x0.08")

# ---------- 3. 目录拉不到 → None，不冒充 ----------
print("[3] 目录失败路径")
gateway._fetch_catalog_raw = lambda auth, uid: None
reset_cache()
check("拉不到时 catalog_display 为 None",
      gateway.catalog_display(store) is None)
gateway._fetch_catalog_raw = lambda auth, uid: copy.deepcopy(SAMPLE)

# ---------- 4. 设置页渲染 ----------
print("[4] 设置页参考区渲染")
reset_cache()
html = server.settings_page().decode("utf-8")
check("Base URL 端点在", "<code>https://koi.ipv64.de/v1</code>" in html)
check("models 端点在", "GET https://koi.ipv64.de/v1/models" in html)
check("chat 端点在", "POST https://koi.ipv64.de/v1/chat/completions" in html)
check("参考区在网关卡下方",
      html.find("网关参考") > html.find("调用密钥（明文") > 0)
check("主力模型行上下文逐字",
      "300000（可选 300000 / 600000 / 1000000）" in html)
check("主力模型行最大输出逐字", "131072" in html)
check("主力模型行积分倍率逐字", "x0.08" in html)
check("视频模型行也在表里", "kling-video-v3" in html)

gateway._fetch_catalog_raw = lambda auth, uid: None
reset_cache()
html2 = server.settings_page().decode("utf-8")
check("目录拉不到时显示提示句", "模型目录暂时拉不到" in html2)
check("拉不到时不出现模型表", "积分倍率（credits）" not in html2)
gateway._fetch_catalog_raw = orig_raw

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
