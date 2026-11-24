# -*- coding: utf-8 -*-
"""WorkBuddy 成长计划（做任务赢积分）自动完成客户端。

接口逆向自官网用户中心「成长计划」页面前端（usercenter web 的 growthSpace API
模块），与页面行为一致（Bearer token 鉴权，走 cb_api._request，同一网关）：

  GET  /v2/activity/growth/tasks                  任务列表（状态机与奖励）
  POST /activity/growth/tasks/accept              {task_codes:[...]} 批量接受任务
  POST /activity/growth/tasks/{code}/claim        领取单个已完成任务的奖励
  POST /activity/growth/buddy/agreement           {agree:true} 同意队长协议
  POST /activity/growth/buddy/first               领取首只 Buddy（first_buddy 奖励）
  GET  /v2/activity/growth/subscribe-task/status  刷新订阅类任务的判定状态
  GET  /activity/growth/energy                    能量余额
  GET  /activity/growth/streak                    本月打卡天数 / 兑换档位资格
  POST /activity/growth/redeem                    {tier,client_token} 打卡档位兑换
  GET  /activity/growth/lottery/chances           抽奖次数余额
  POST /activity/growth/lottery/draw              {client_token} 抽奖

派猫（Buddy 旅行，1~4 小时往返，产出积分）与盲盒（消耗能量开新 Buddy）：
  GET  /activity/growth/buddy/travel/config       目的地列表（id/name/时长/积分区间）
  GET  /activity/growth/buddy/travel/status       state: idle/traveling/arrived
                                                  + daily_limit_reached（每日派猫次数上限）
  POST /activity/growth/buddy/travel/depart       {location_id} 出发
  POST /activity/growth/buddy/travel/claim        到达后领取（积分 + 信）
  GET  /activity/growth/buddy/quota               {balance, cost_per_open, affordable,
                                                  max_open_count} 盲盒配额
  POST /activity/growth/buddy/open                {count} 开盲盒（耗能量 cost_per_open/只）

任务状态机：not_accepted → accepted → in_progress → completed → claimed。
- first_buddy 是 auto 型任务（服务端判完成），领取即得首只 Buddy + 积分 + 能量，
  且是其余任务的前置条件（未领取时 accept 报 "prerequisite not met"）；
- 其余任务需账号在 WorkBuddy 里发生真实使用行为（对话/建画布/用模板等）才推进
  进度，服务端按埋点判定。本工具只做「接受任务 + 领取已完成奖励 + 打卡兑换 +
  抽奖 + 派猫 + 开盲盒」，不伪造行为埋点。

自动化循环（growth-all / 每日计划任务）：
  领任务奖励 → 派猫（arrived 先领、idle 且未达每日上限就出发）→
  能量 ≥ cost_per_open 的倍数就开盲盒（能量来自任务/首只奖励，派猫产出积分）。
"""
from __future__ import annotations

import random
import time
import uuid

import cb_api

TASKS_PATH = "/v2/activity/growth/tasks"
SUBSCRIBE_STATUS_PATH = "/v2/activity/growth/subscribe-task/status"
ACCEPT_PATH = "/activity/growth/tasks/accept"
CLAIM_PATH = "/activity/growth/tasks/{code}/claim"
AGREEMENT_PATH = "/activity/growth/buddy/agreement"
BUDDY_FIRST_PATH = "/activity/growth/buddy/first"
BUDDY_INFO_PATH = "/activity/growth/buddy/info"
ENERGY_PATH = "/activity/growth/energy"
STREAK_PATH = "/activity/growth/streak"
REDEEM_PATH = "/activity/growth/redeem"
LOTTERY_CHANCES_PATH = "/activity/growth/lottery/chances"
LOTTERY_DRAW_PATH = "/activity/growth/lottery/draw"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
BUDDY_QUOTA_PATH = "/activity/growth/buddy/quota"
BUDDY_OPEN_PATH = "/activity/growth/buddy/open"

FIRST_TASK = "first_buddy"

# 打卡兑换档位（tier 名与 redemptions_status 的 tier_XXd_status 对应）
REDEEM_TIERS = ("28d", "14d", "7d")  # 奖励多的优先
DRAW_MAX = 5  # 单次最多抽几下（防死循环）
OPEN_MAX = 5  # 单次最多开几个盲盒（服务端 max_open_count 也是 5）


def _call(method: str, path: str, auth: dict, body=None) -> dict:
    """请求并返回 data；业务失败抛 ApiError（带服务端 msg）。"""
    resp = cb_api._request(method, path, body=body, auth=auth, timeout=20)
    if resp.get("code") != 0:
        raise cb_api.ApiError(f"code={resp.get('code')} {resp.get('msg') or ''}",
                              code=resp.get("code"))
    return resp.get("data") or {}


def _reward(data: dict) -> tuple[int, int]:
    """从领奖/兑换响应里尽量抠出 (credit, energy)。"""
    credit = data.get("credit") or data.get("reward_credit") or 0
    energy = data.get("energy") or data.get("reward_energy") or 0
    try:
        return int(credit or 0), int(energy or 0)
    except (TypeError, ValueError):
        return 0, 0


def _extract_buddies(data: object) -> list[dict]:
    """在任意响应结构里递归找 buddy instance（name+instance_id/rarity 特征）。

    开盲盒/领任务奖的响应结构带 buddy，但字段层级不固定（instance/template
    嵌套或扁平），按特征匹配最稳。
    """
    out: list[dict] = []

    def walk(v) -> None:
        if isinstance(v, dict):
            if v.get("name") and ("instance_id" in v or "rarity" in v):
                out.append(v)
                return
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(data)
    return out


def _buddy_label(inst: dict) -> str:
    """buddy instance 压成 `名字(稀有度)`。"""
    return f"{inst.get('name') or '?'}({inst.get('rarity') or '?'})"


def fetch_tasks(auth: dict) -> list[dict]:
    """任务列表。"""
    return (_call("GET", TASKS_PATH, auth) or {}).get("tasks") or []


def overview(auth: dict) -> dict:
    """只读汇总：任务、能量、打卡、抽奖次数。返回 {ok, ...}。"""
    out: dict = {"ok": False, "error": ""}
    try:
        out["tasks"] = fetch_tasks(auth)
        out["energy"] = (_call("GET", ENERGY_PATH, auth) or {}).get("balance") or 0
        streak = _call("GET", STREAK_PATH, auth) or {}
        out["streak"] = streak.get("streak") or {}
        out["redemption"] = streak.get("redemption_status") or {}
        out["lottery_chances"] = (_call("GET", LOTTERY_CHANCES_PATH, auth)
                                  or {}).get("balance") or 0
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    return out


def travel_step(auth: dict) -> dict:
    """派猫一步：arrived 先领、idle 且未达每日上限就随机选地出发。

    返回 {action: claimed/departed/traveling/limit/none, credit, location,
          arrive_in_sec, daily_limit_reached}。
    页面里出发地由用户挑，这里随机选一个（等价于人工任选）。
    """
    out = {"action": "none", "credit": 0, "location": "",
           "arrive_in_sec": 0, "daily_limit_reached": False}
    st = _call("GET", TRAVEL_STATUS_PATH, auth) or {}
    out["daily_limit_reached"] = bool(st.get("daily_limit_reached"))
    state = st.get("state") or "idle"

    if state == "arrived":
        data = _call("POST", TRAVEL_CLAIM_PATH, auth, {}) or {}
        loc = (data.get("location") or st.get("location") or {})
        out.update(action="claimed",
                   credit=int(data.get("reward_credit")
                              or st.get("reward_credit") or 0),
                   location=loc.get("name") or "")
        # 领完立刻看能不能再派（未达每日上限则继续）
        st = _call("GET", TRAVEL_STATUS_PATH, auth) or {}
        state = st.get("state") or "idle"
        out["daily_limit_reached"] = bool(st.get("daily_limit_reached"))

    if state == "idle" and not out["daily_limit_reached"]:
        locations = (_call("GET", TRAVEL_CONFIG_PATH, auth)
                     or {}).get("locations") or []
        if locations:
            loc = random.choice(locations)
            data = _call("POST", TRAVEL_DEPART_PATH, auth,
                         {"location_id": loc.get("id")}) or {}
            arrive = int(data.get("arrive_at") or 0)
            now = int(data.get("server_now") or 0)
            out.update(action="departed" if out["action"] != "claimed"
                       else "claimed+departed",
                       location=loc.get("name") or "",
                       arrive_in_sec=max(0, arrive - now) if arrive else 0)
    elif state == "traveling":
        loc = st.get("location") or {}
        arrive = int(st.get("arrive_at") or 0)
        now = int(st.get("server_now") or 0)
        out.update(action="traveling",
                   location=loc.get("name") or "",
                   arrive_in_sec=max(0, arrive - now) if arrive else 0)
    elif state == "idle" and out["daily_limit_reached"]:
        out["action"] = "limit"
    return out


def open_blind_box(auth: dict) -> dict:
    """能量够了就开盲盒（每只耗 cost_per_open 能量，最多 OPEN_MAX 只）。

    返回 {opened: n, new_buddies: [name...], cost, balance}。
    """
    out = {"opened": 0, "new_buddies": [], "cost": 0, "balance": 0}
    quota = _call("GET", BUDDY_QUOTA_PATH, auth) or {}
    affordable = int(quota.get("affordable") or 0)
    out["balance"] = int(quota.get("balance") or 0)
    out["cost"] = int(quota.get("cost_per_open") or 0)
    if affordable <= 0:
        return out
    count = min(affordable, OPEN_MAX, int(quota.get("max_open_count") or 1))
    data = _call("POST", BUDDY_OPEN_PATH, auth, {"count": count}) or {}
    out["new_buddies"] = [_buddy_label(i) for i in _extract_buddies(data)]
    out["opened"] = len(out["new_buddies"]) or count
    return out


def run(auth: dict, *, accept: bool = True, claim: bool = True,
        redeem: bool = True, draw: bool = True, endpoint: str | None = None) -> dict:
    """自动完成成长计划：接受任务、领奖、打卡兑换、抽奖。返回汇总。

    全程幂等：已接受的任务跳过、已领奖跳过、兑换/抽奖以资格判定为准。
    """
    res = {
        "ok": False, "error": "", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "first": None,            # 首只 Buddy 领取结果（None=已领过或无需）
        "accepted": [],           # 本次新接受的任务 code
        "claimed": [],            # [{code, credit, energy}]
        "redeemed": [],           # [tier]
        "drawn": [],              # 抽奖奖品列表
        "pending": [],            # 已接受但未完成（需真实行为）的 code
        "locked": [],             # 未解锁的任务 code
        "energy": 0, "lottery_chances": 0,
        "travel": None,           # 派猫：{action, credit, location, arrive_in_sec}
        "box": None,              # 盲盒：{opened, new_buddies, cost, balance}
        "completed": 0, "total": 0, "level_name": "",
    }
    try:
        # 0) 刷新订阅类任务判定（服务端按当前关注状态更新任务状态）
        try:
            _call("GET", SUBSCRIBE_STATUS_PATH, auth)
        except Exception:  # noqa: BLE001
            pass

        # 1) 领取首只 Buddy（其余任务的前置条件）
        tasks = fetch_tasks(auth)
        first = next((t for t in tasks if t.get("task_code") == FIRST_TASK), None)
        if first and first.get("accept_status") == "completed":
            _call("POST", AGREEMENT_PATH, auth, {"agree": True})
            data = _call("POST", BUDDY_FIRST_PATH, auth, {})
            credit, energy = _reward(data)
            name, rarity = "", ""
            try:  # 领取响应里 instance 不带名字，从 buddy/info 补
                info = _call("GET", BUDDY_INFO_PATH, auth) or {}
                inst = info.get("buddy") or {}
                name, rarity = inst.get("name") or "", inst.get("rarity") or ""
            except Exception:  # noqa: BLE001
                pass
            res["first"] = {"credit": credit, "energy": energy,
                            "buddy": name, "rarity": rarity}

        # 2) 批量接受所有待接受且未锁定的任务
        if accept:
            codes = [t["task_code"] for t in fetch_tasks(auth)
                     if t.get("task_code") != FIRST_TASK
                     and t.get("accept_status") == "not_accepted"
                     and not t.get("locked")]
            for i in range(0, len(codes), 10):  # 分批，避免一次过长
                batch = codes[i:i + 10]
                data = _call("POST", ACCEPT_PATH, auth, {"task_codes": batch})
                for r in (data.get("results") or []):
                    if r.get("status") in ("accepted", "completed", "claimed"):
                        res["accepted"].append(r.get("task_code"))

        # 3) 领取所有已完成任务的奖励
        if claim:
            for t in fetch_tasks(auth):
                if t.get("accept_status") != "completed" or not t.get("has_reward"):
                    continue
                code = t["task_code"]
                try:
                    if code == FIRST_TASK:
                        data = _call("POST", BUDDY_FIRST_PATH, auth, {})
                    else:
                        data = _call("POST", CLAIM_PATH.format(code=code), auth, {})
                    credit, energy = _reward(data)
                    buddies = [_buddy_label(i) for i in _extract_buddies(data)]
                    res["claimed"].append({"code": code, "credit": credit,
                                           "energy": energy,
                                           "buddies": buddies})
                except Exception as e:  # noqa: BLE001
                    res["claimed"].append({"code": code, "credit": 0, "energy": 0,
                                           "message": str(e)[:120]})

        # 4) 打卡档位兑换（available 才兑，从奖励高的档位开始）
        if redeem:
            redemption = (_call("GET", STREAK_PATH, auth) or {}).get(
                "redemption_status") or {}
            for tier in REDEEM_TIERS:
                if redemption.get(f"tier_{tier}_status") == "available":
                    _call("POST", REDEEM_PATH, auth,
                          {"tier": tier, "client_token": str(uuid.uuid4())})
                    res["redeemed"].append(tier)

        # 5) 抽奖（有次数就抽完）
        if draw:
            for _ in range(DRAW_MAX):
                balance = (_call("GET", LOTTERY_CHANCES_PATH, auth) or {}).get(
                    "balance") or 0
                if not balance:
                    break
                data = _call("POST", LOTTERY_DRAW_PATH, auth,
                             {"client_token": str(uuid.uuid4())})
                prize = (data.get("prize") or data.get("reward") or data or {})
                name = (prize.get("name") or prize.get("prize_name")
                        if isinstance(prize, dict) else None)
                res["drawn"].append(name or "已抽奖")

        # 6) 派猫：到达先领，没出门就出发（每日上限内）
        try:
            res["travel"] = travel_step(auth)
        except Exception as e:  # noqa: BLE001
            res["travel"] = {"action": "error", "message": str(e)[:120]}

        # 7) 盲盒：能量 ≥ cost_per_open 的倍数就开（含前面领到的能量）
        try:
            res["box"] = open_blind_box(auth)
        except Exception as e:  # noqa: BLE001
            res["box"] = {"opened": 0, "new_buddies": [], "message": str(e)[:120]}

        # 8) 汇总状态
        ov = overview(auth)
        if ov.get("ok"):
            tasks = ov["tasks"] or []
            done = sum(1 for t in tasks if t.get("accept_status") in ("completed",
                                                                     "claimed"))
            res.update(energy=ov.get("energy") or 0,
                       lottery_chances=ov.get("lottery_chances") or 0,
                       completed=done, total=len(tasks))
            res["pending"] = [t["task_code"] for t in tasks
                              if t.get("accept_status") in ("accepted", "in_progress")]
            res["locked"] = [t["task_code"] for t in tasks if t.get("locked")]
        else:
            res["error"] = ov.get("error") or ""
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:200]
    return res


def result_text(res: dict) -> str:
    """把 run() 结果压成一行明细（GUI 列表 / CLI 用）。"""
    if not res.get("ok"):
        return f"失败：{res.get('error') or '未知错误'}"
    parts = []
    first = res.get("first")
    if first:
        parts.append(f"首只Buddy「{first.get('buddy') or '?'}」"
                     f"+{first.get('credit') or 0}积分")
    n = len(res.get("claimed") or [])
    if n:
        credit = sum(c.get("credit") or 0 for c in res["claimed"])
        buddies = [b for c in res["claimed"] for b in (c.get("buddies") or [])]
        txt = f"领奖{n}个+{credit}积分"
        if buddies:
            txt += "得" + "、".join(buddies)
        parts.append(txt)
    if res.get("accepted"):
        parts.append(f"接受{len(res['accepted'])}个任务")
    if res.get("redeemed"):
        parts.append("兑换" + "/".join(res["redeemed"]))
    if res.get("drawn"):
        parts.append(f"抽奖x{len(res['drawn'])}")
    travel = res.get("travel") or {}
    act = travel.get("action") or ""
    loc = travel.get("location") or ""
    if "claimed" in act:
        parts.append(f"派猫领{travel.get('credit') or 0}积分"
                     + (f"（{loc}）" if loc else ""))
    if act in ("departed", "claimed+departed"):
        hrs = (travel.get("arrive_in_sec") or 0) / 3600
        parts.append(f"已派往{loc or '旅行'}"
                     + (f"约{hrs:.0f}h后回" if hrs >= 1 else ""))
    if act == "limit":
        parts.append("派猫今日已满")
    if act == "traveling":
        hrs = (travel.get("arrive_in_sec") or 0) / 3600
        parts.append(f"旅行中（{loc or '?'}，约{hrs:.0f}h后回）")
    box = res.get("box") or {}
    if box.get("opened"):
        names = "、".join(box.get("new_buddies") or [])
        parts.append(f"开盲盒x{box['opened']}" + (f"得{names}" if names else ""))
    if not parts:
        parts.append("无新完成")
    txt = "，".join(parts)
    if res.get("total"):
        txt += f"（{res.get('completed')}/{res['total']}）"
    pending = res.get("pending") or []
    if pending:
        txt += f"；{len(pending)}个待完成真实行为"
    return txt
