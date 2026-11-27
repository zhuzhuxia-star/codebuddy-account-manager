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

# ---------- 2. /v1/models 与官方目录同口径（数值化、不滤除） ----------
print("[2] list_models 同源且与目录同口径")
reset_cache()
data = gateway.list_models(store)
tm = {m["id"]: m for m in data["data"]}
check("裁剪版 glm-5.3-flash context_length 为数值 defaultLength",
      (tm.get("glm-5.3-flash") or {}).get("context_length") == 300000,
      str((tm.get("glm-5.3-flash") or {}).get("context_length")))
check("裁剪版条数 == 样本目录总数（不滤除）",
      len(data["data"]) == len(SAMPLE),
      f"{len(data['data'])} vs {len(SAMPLE)}")
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

# ---------- 5. 思考档位列 + 表头排序（二期追加） ----------
print("[5] 思考档位列与表头点击排序")
reset_cache()
rows = gateway.catalog_display(store)
by_id = {r["id"]: r for r in rows or []}
sample_by_id = {m["id"]: m for m in SAMPLE if m.get("id")}
g = by_id.get("glm-5.3-flash") or {}
gs = (sample_by_id.get("glm-5.3-flash") or {}).get("reasoning") or {}
check("思考档位列字段存在",
      "reasoning_supported" in g and "reasoning_default" in g
      and "reasoning_can_disable" in g)
check("glm-5.3-flash 支持档位与样本逐字一致",
      g.get("reasoning_supported") == gs.get("supportedEfforts"),
      f"{g.get('reasoning_supported')} vs {gs.get('supportedEfforts')}")
check("glm-5.3-flash 默认档位与样本逐字一致",
      g.get("reasoning_default") == gs.get("defaultEffort"),
      f"{g.get('reasoning_default')} vs {gs.get('defaultEffort')}")
check("glm-5.3-flash 可关思考与样本逐字一致",
      g.get("reasoning_can_disable") == gs.get("canDisableThinking"),
      f"{g.get('reasoning_can_disable')} vs {gs.get('canDisableThinking')}")
a = by_id.get("auto") or {}
check("auto 只有固定 effort、无档位列表",
      a.get("reasoning_supported") is None
      and a.get("reasoning_default") == "high",
      str({k: a.get(k) for k in ("reasoning_supported", "reasoning_default")}))
k = by_id.get("kling-video-v3") or {}
check("目录没给 reasoning 的模型档位全空",
      k.get("reasoning_supported") is None
      and k.get("reasoning_default") is None
      and k.get("reasoning_can_disable") is None)
check("排序键：上下文数值=默认档位数", g.get("context_num") == 300000,
      str(g.get("context_num")))
check("排序键：倍率数值=x 后数字", g.get("credits_num") == 0.08,
      str(g.get("credits_num")))
check("排序键：思考权重 high=3", g.get("reasoning_rank") == 3,
      str(g.get("reasoning_rank")))

reset_cache()
html = server.settings_page().decode("utf-8")
check("思考档位列表头在", "思考档位" in html)
check("思考档位值渲染（档位+默认）",
      "low / high / max（默认 high）" in html)
check("排序 JS 存在", "function gwSort" in html)
check("五个列头都带排序绑定",
      html.count('onclick="gwSort(this)"') == 5,
      str(html.count('onclick="gwSort(this)"')))
check("模型表带 id", 'id="gw-models"' in html)
check("数值单元格带 data-v 排序键", 'data-v="300000"' in html
      and 'data-v="0.08"' in html)

# ---------- 6. 模型列显示名（软件界面名） ----------
print("[6] 模型列显示名")
reset_cache()
rows = gateway.catalog_display(store)
by_id = {r["id"]: r for r in rows or []}
g = by_id.get("glm-5.3-flash") or {}
check("glm-5.3-flash 显示名 = GLM-5.3-Flash",
      g.get("display_name") == "GLM-5.3-Flash", str(g.get("display_name")))
hf = by_id.get("hy4flash-free") or {}
check("hy4flash-free 显示名 = Hy4 flash",
      hf.get("display_name") == "Hy4 flash", str(hf.get("display_name")))
qa = by_id.get("quick-answer") or {}
check("quick-answer 显示名与 id 相同",
      qa.get("display_name") == "quick-answer", str(qa.get("display_name")))
check("模型列排序键 = 显示名小写",
      g.get("name_sort") == "glm-5.3-flash"
      and hf.get("name_sort") == "hy4 flash",
      f"{g.get('name_sort')} / {hf.get('name_sort')}")
check("显示名与样本 name 逐字一致（全量）",
      all(r["display_name"] == str(sample_by_id[r["id"]].get("name") or r["id"])
          for r in rows or []))

reset_cache()
html = server.settings_page().decode("utf-8")
check("页面主显示为软件显示名", "GLM-5.3-Flash" in html and "Hy4 flash" in html)
glm_row = next((seg for seg in html.split("<tr>") if "GLM-5.3-Flash" in seg), "")
check("glm 行同行弱化跟随调用名 id", "glm-5.3-flash" in glm_row)
check("模型列单元格排序键 = 显示名小写", 'data-v="hy4 flash"' in html)
qa_row = next((seg for seg in html.split("<tr>")
               if 'data-v="quick-answer"' in seg), "")
check("quick-answer 行名=id 只显示一个（不跟随重复 id）",
      qa_row != "" and "（<code>quick-answer</code>）" not in qa_row)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
