# -*- coding: utf-8 -*-
"""积分获取记账：签到 / 成长任务 / 连登兑换 / 抽奖 / 猫猫旅行 / 积分补偿 /
新手礼包 每领到一笔积分就记一行，供网页版「积分获取」页查询。

存储：DATA_DIR/ledger.json（与账号库同目录），结构 {entries: [...]}，
每条 {ts, date, uid, label, phone_tail, source, detail, credit, note}。
credit 为 None 表示这笔数量未知（接口没返回确切数），不参与金额汇总，
只计笔数。历史数据无法回填，本账本自上线起记录。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
LEDGER_FILE = DATA_DIR / "ledger.json"
MAX_ENTRIES = 8000  # 超出后丢最旧的，防止文件无限长

SOURCES = {
    "checkin": "签到",
    "task": "成长任务",
    "redeem": "连登兑换",
    "lottery": "抽奖",
    "travel": "猫猫旅行",
    "compensation": "积分补偿",
    "gift": "新手礼包",
    "other": "其他",
}

_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else data
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(
        json.dumps({"entries": entries[-MAX_ENTRIES:]},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")


def add(uid: str, label: str, phone_tail: str, source: str, detail: str = "",
        credit=None, note: str = "") -> dict:
    """记一笔。credit 传 None 表示数量未知。返回写入的条目。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "ts": now,
        "date": now[:10],
        "uid": str(uid or ""),
        "label": str(label or ""),
        "phone_tail": str(phone_tail or ""),
        "source": source if source in SOURCES else "other",
        "detail": str(detail or ""),
        "credit": credit,
        "note": str(note or ""),
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        _save(entries)
    return entry


def list_entries(uid: str = "", source: str = "", limit: int = 300) -> list[dict]:
    """按时间倒序取条目，可按账号 uid / 来源筛选。"""
    with _lock:
        entries = _load()
    out = [e for e in reversed(entries)
           if (not uid or e.get("uid") == uid)
           and (not source or e.get("source") == source)]
    return out[:limit]


def accounts_in_entries() -> list[dict]:
    """账本里出现过的账号（uid -> 最近一次的 label/phone_tail），供筛选下拉。"""
    with _lock:
        entries = _load()
    seen: dict[str, dict] = {}
    for e in entries:
        if e.get("uid"):
            seen[e["uid"]] = {"uid": e["uid"], "label": e.get("label") or "",
                              "phone_tail": e.get("phone_tail") or ""}
    return list(seen.values())


def summary() -> dict:
    """汇总：今日 / 累计金额与未知笔数、按账号小计、按来源小计。

    金额只统计 credit 非 None 的条目；未知数量的单独计笔数。
    """
    today = time.strftime("%Y-%m-%d")
    with _lock:
        entries = _load()
    out = {
        "today_credit": 0, "today_unknown": 0, "today_count": 0,
        "total_credit": 0, "total_unknown": 0, "total_count": len(entries),
        "per_account": {},   # uid -> {label, phone_tail, credit, unknown, count}
        "per_source": {},    # source -> {credit, unknown, count}
    }
    for e in entries:
        credit = e.get("credit")
        known = isinstance(credit, (int, float)) and not isinstance(credit, bool)
        amt = credit if known else 0
        out["total_credit"] += amt
        out["total_unknown"] += 0 if known else 1
        if e.get("date") == today:
            out["today_credit"] += amt
            out["today_unknown"] += 0 if known else 1
            out["today_count"] += 1
        pa = out["per_account"].setdefault(
            e.get("uid") or "", {"label": e.get("label") or "",
                                 "phone_tail": e.get("phone_tail") or "",
                                 "credit": 0, "unknown": 0, "count": 0})
        pa["credit"] += amt
        pa["unknown"] += 0 if known else 1
        pa["count"] += 1
        if e.get("label"):
            pa["label"] = e["label"]
        ps = out["per_source"].setdefault(
            e.get("source") or "other", {"credit": 0, "unknown": 0, "count": 0})
        ps["credit"] += amt
        ps["unknown"] += 0 if known else 1
        ps["count"] += 1
    return out
