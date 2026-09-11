# -*- coding: utf-8 -*-
"""WorkBuddy 积分签到接口客户端。

接口与 WorkBuddy 客户端（Desktop 走 IDE 网关，需 /v2 前缀）一致：

  1) POST /v2/billing/meter/checkin-activity-status   查询签到活动与今日状态
  2) POST /v2/billing/meter/daily-checkin             每日签到（领取积分）

两者均为空 body `{}`，Bearer token 放在 Authorization 头（由 cb_api._request 处理），
endpoint 与 CodeBuddy 同一网关（cb_api.resolve_endpoint）。

响应 `code == 0` 视为成功；业务码与客户端 CheckinClaimStatus 对齐：
  1001 今日已领取 / 1002 不符合领取条件 / 1003 活动已结束。
"""
from __future__ import annotations

import json
import time

import cb_api

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"

# 领取状态（对齐客户端 CheckinClaimStatus）
CLAIMED = "claimed"
ALREADY_CLAIMED = "already_claimed"
NOT_ELIGIBLE = "not_eligible"
EVENT_ENDED = "event_ended"
UNKNOWN_BIZ_ERROR = "unknown_biz_error"
REQUEST_ERROR = "request_error"

# 实测服务端用 5 位码（10001 今天已签到），客户端源码里记的是 1001 系列，两者都兼容
BIZ_CODES = {
    1001: ALREADY_CLAIMED, 1002: NOT_ELIGIBLE, 1003: EVENT_ENDED,
    10001: ALREADY_CLAIMED, 10002: NOT_ELIGIBLE, 10003: EVENT_ENDED,
}
STATUS_LABELS = {
    CLAIMED: "签到成功",
    ALREADY_CLAIMED: "今日已签到",
    NOT_ELIGIBLE: "不符合签到条件",
    EVENT_ENDED: "活动已结束",
    UNKNOWN_BIZ_ERROR: "未知业务错误",
    REQUEST_ERROR: "请求失败",
}


def _map_status(code) -> str:
    try:
        return BIZ_CODES.get(int(code), UNKNOWN_BIZ_ERROR)
    except (TypeError, ValueError):
        return UNKNOWN_BIZ_ERROR


def _server_msg(text: str) -> str:
    """从异常文本里抠出服务端 msg（形如 `HTTP 400: {"code":10001,"msg":"..."}`）。"""
    i = text.find("{")
    if i < 0:
        return ""
    try:
        return str(json.loads(text[i:]).get("msg") or "")
    except Exception:
        return ""


def fetch_status(auth: dict, endpoint: str | None = None) -> dict:
    """查询签到活动状态。返回 {ok, data, error}。"""
    out = {"ok": False, "data": None, "error": ""}
    try:
        resp = cb_api._request("POST", STATUS_PATH, body={}, auth=auth,
                               timeout=20, endpoint=endpoint)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
        return out
    if resp.get("code") == 0:
        out["ok"] = True
        out["data"] = resp.get("data") or {}
    else:
        out["error"] = f"code={resp.get('code')} {resp.get('msg') or ''}".strip()
    return out


def checkin(auth: dict, endpoint: str | None = None) -> dict:
    """每日签到（领取积分）。返回 {ok, status, label, message, credit, streak_days, ...}。"""
    out = {
        "ok": False, "status": REQUEST_ERROR, "label": "", "message": "",
        "credit": 0, "streak_days": 0, "is_streak_day": False, "data": None,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        resp = cb_api._request("POST", CHECKIN_PATH, body={}, auth=auth,
                               timeout=20, endpoint=endpoint)
    except cb_api.ApiError as e:
        # 服务端业务码（如 10001 今天已签到）也会带 HTTP 400，按业务结果处理
        out["status"] = _map_status(e.code)
        out["message"] = _server_msg(str(e)) or ""
        out["label"] = STATUS_LABELS.get(out["status"], out["message"] or "失败")
        return out
    except Exception as e:  # noqa: BLE001
        out["message"] = str(e)[:200]
        out["label"] = STATUS_LABELS[REQUEST_ERROR]
        return out

    code = resp.get("code")
    data = resp.get("data") or {}
    if code == 0:
        out.update(ok=True, status=CLAIMED, data=data,
                   credit=data.get("credit") or 0,
                   streak_days=data.get("streak_days") or 0,
                   is_streak_day=bool(data.get("is_streak_day")))
    else:
        out["status"] = _map_status(code)
        out["message"] = resp.get("msg") or ""
    out["label"] = STATUS_LABELS.get(out["status"], out["message"] or "失败")
    return out


def status_text(data: dict | None) -> str:
    """把 checkin-activity-status 的 data 压成一行展示文本。"""
    if not data:
        return "-"
    if not data.get("active"):
        return "活动未开启"
    if data.get("today_checked_in"):
        return f"今日已签到 · 连续 {data.get('streak_days') or 0} 天"
    credit = data.get("today_credit") or data.get("daily_credit") or 0
    return f"未签到 · 可领 {credit}"


def result_text(res: dict) -> str:
    """把 checkin() 的结果压成一行明细。"""
    if res.get("ok"):
        txt = f"{res.get('label')} +{res.get('credit') or 0} 积分"
        if res.get("streak_days"):
            txt += f"（连续 {res['streak_days']} 天）"
        return txt
    msg = res.get("message") or ""
    return f"{res.get('label')}{('：' + msg) if msg else ''}"
