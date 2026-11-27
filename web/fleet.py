# -*- coding: utf-8 -*-
"""第三方设备接入（fleet）：设备心跳 + 短信上报 API 的数据层。

与网关（gateway.py）平级、互不干扰：
  - 接入密钥单独一把，存 DATA_DIR/fleet.json（权限 0600，明文只在
    服务端与管理页出现），服务端另存 sha256 哈希校验。请求带
    X-Device-Key 头，或 Authorization: Bearer <key>。管理页可自定义
    改密钥或随机换一把，换后旧密钥立即失效。
  - IMEI 即设备身份（本轮不做逐设备密钥）。
  - 存储 DATA_DIR/fleet.db（SQLite）：devices / sms 两张表。
  - 短信只保留 SMS_TTL 秒（1 小时）：读列表与新短信入库时顺带清理
    过期短信，管理页也可手动删单条 / 清空全部。
  - 设备可由管理页编辑手机号 / 类型 / 备注（type 也可随心跳上报）。
  - 设备记 online_since（本次连续在线的起始时间）：从离线转在线时
    打点、持续在线不刷新、上报离线时清空，页面据此显示「在线时长」。
  - 短信时间分两列：上传时间 = received_at（服务器收到时间）、
    短信时间 = device_ts（设备上报的短信时间，没报 ts 时与上传相同）。
  - 两个接口的每次请求在 fleet_access.log 追加一行脱敏日志（北京
    时间/接口/状态码/IMEI 后 4 位/是否带密钥头/Content-Type/请求体
    字段名列表），绝不记密钥值、短信内容、完整手机号与完整 IMEI；
    写日志失败静默，永不影响接口本身。管理页底部「最近请求」展示。

在线判定：last_seen 在 ONLINE_WINDOW 秒内且最近一次上报 online=true。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
KEY_FILE = DATA_DIR / "fleet.json"
DB_FILE = DATA_DIR / "fleet.db"
LOG_FILE = DATA_DIR / "fleet_access.log"

ONLINE_WINDOW = 180  # 秒：最后心跳在此窗口内且上报在线才算在线
SMS_TTL = 3600  # 秒：短信只保留 1 小时，过期自动清理
PAGE_SIZE = 20  # 管理页设备表 / 短信表每页条数

# ---------- 接入密钥 ----------
_key_lock = threading.Lock()
_db_lock = threading.Lock()


def _save_key(plain: str) -> dict:
    """把接入密钥明文 + sha256 哈希落盘 fleet.json（0600），返回配置。"""
    conf = {
        "key_plain": plain,
        "key_hash": hashlib.sha256(plain.encode("utf-8")).hexdigest(),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return conf


def _key_conf(create: bool = False) -> dict:
    try:
        data = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("key_plain"):
            return data
    except Exception:
        pass
    if not create:
        return {}
    return _save_key("sk-fleet-" + secrets.token_urlsafe(32))


def ensure_key() -> str:
    """确保已生成接入密钥，返回明文（只在服务端内部/管理页用）。"""
    with _key_lock:
        return _key_conf(create=True).get("key_plain") or ""


def key_plain() -> str:
    return ensure_key()


def key_masked() -> str:
    plain = ensure_key()
    if len(plain) <= 12:
        return plain[:3] + "••••"
    return f"{plain[:10]}••••••{plain[-4:]}"


def _key_ok(token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    conf = _key_conf(create=True)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, conf.get("key_hash") or "")


def check_key(device_key_header: str, authorization_header: str) -> bool:
    """接入鉴权：X-Device-Key 头或 Authorization: Bearer 任一命中接入密钥。"""
    if _key_ok(device_key_header or ""):
        return True
    auth = (authorization_header or "").strip()
    if auth.startswith("Bearer "):
        return _key_ok(auth[len("Bearer "):])
    return False


# 自定义密钥只允许常见可见 ASCII：字母、数字与 -_.!@#$%^&*+=:;,?~/| 等符号。
_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.!@#$%^&*+=:;,?~/|]+$")


def validate_new_key(v) -> str:
    """校验管理页自定义的新接入密钥：合格返回 ''，不合格返回中文原因。"""
    if not isinstance(v, str):
        return "密钥格式不对"
    v = v.strip()
    if len(v) < 8:
        return "至少 8 位"
    if any(c.isspace() for c in v):
        return "不能含空白字符"
    if not _KEY_RE.match(v):
        return "只能用字母、数字和常见符号（-_.!@#$%^&*+=:;,?~/|）"
    return ""


def set_custom_key(v) -> tuple:
    """管理页设置自定义接入密钥：校验通过才落盘（明文+哈希同步），
    旧密钥立即失效；校验失败不改变现有密钥。返回 (是否成功, 失败原因)。"""
    err = validate_new_key(v)
    if err:
        return False, err
    with _key_lock:
        _save_key(v.strip())
    return True, ""


def rotate_key() -> str:
    """随机换一把接入密钥（sk-fleet- 前缀），旧密钥立即失效，返回新明文。"""
    with _key_lock:
        return _save_key("sk-fleet-" + secrets.token_urlsafe(32))["key_plain"]


# ---------- 存储 ----------
def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS devices("
        "imei TEXT PRIMARY KEY, phone TEXT, online_reported INTEGER DEFAULT 0,"
        "last_seen REAL, model TEXT, battery TEXT, app_version TEXT)")
    # 迁移：老库补 type（设备类型）/ remark（备注）/ online_since
    # （本次连续在线的起始时间）三列，已有数据不动。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if "type" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN type TEXT")
    if "remark" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN remark TEXT")
    if "online_since" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN online_since REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sms("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, imei TEXT NOT NULL,"
        "sender TEXT, content TEXT NOT NULL, device_ts REAL,"
        "received_at REAL NOT NULL)")
    return conn


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def fmt_duration(sec) -> str:
    """秒数 → 人话时长：刚刚 / 25分钟 / 2小时15分 / 3天4小时。"""
    sec = int(sec)
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60}分钟"
    if sec < 86400:
        h, m = sec // 3600, (sec % 3600) // 60
        return f"{h}小时{m}分" if m else f"{h}小时"
    d, h = sec // 86400, (sec % 86400) // 3600
    return f"{d}天{h}小时" if h else f"{d}天"


def _clean_imei(body: dict):
    v = body.get("imei")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(int(v))
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _opt_str(body: dict, name: str):
    v = body.get(name)
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _parse_online(body: dict) -> bool:
    if "online" not in body or body.get("online") is None:
        return True
    v = body.get("online")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def _parse_device_ts(body: dict, fallback: float) -> float:
    """设备侧时间戳：数字按毫秒（>1e11）/秒识别，字符串按 ISO 解析，
    都不行就用服务器时间。"""
    v = body.get("ts")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = float(v)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        return ts if ts > 0 else fallback
    if isinstance(v, str) and v.strip():
        s = v.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.astimezone()  # 无时区按本地时间理解
            return dt.timestamp()
        except Exception:
            return fallback
    return fallback


# ---------- 接口 1：心跳 ----------
def heartbeat(body: dict):
    """POST /api/v1/heartbeat：按 imei upsert 设备记录。

    在线时长计时：online=true 且设备此前算离线（未上报在线、心跳已超
    ONLINE_WINDOW、或还没有 online_since）时，online_since 记为本次
    时间，重新开始计时；持续在线的心跳不刷新 online_since；
    online=false 时清空 online_since。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "请求体不是合法 JSON"}
    imei = _clean_imei(body)
    if not imei:
        return 400, {"ok": False, "error": "imei 必填"}
    now = time.time()
    online = _parse_online(body)
    battery = body.get("battery")
    battery_s = None if battery is None else str(battery)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT online_reported, last_seen, online_since"
                " FROM devices WHERE imei=?", (imei,)).fetchone()
            if online:
                was_offline = (
                    row is None or not row["online_reported"]
                    or not row["last_seen"]
                    or (now - float(row["last_seen"])) > ONLINE_WINDOW
                    or not row["online_since"])
                online_since = (now if was_offline
                                else float(row["online_since"]))
            else:
                online_since = None
            conn.execute(
                "INSERT INTO devices(imei, phone, online_reported, last_seen,"
                " model, battery, app_version, type, online_since)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(imei) DO UPDATE SET"
                " phone=COALESCE(excluded.phone, devices.phone),"
                " online_reported=excluded.online_reported,"
                " last_seen=excluded.last_seen,"
                " model=COALESCE(excluded.model, devices.model),"
                " battery=COALESCE(excluded.battery, devices.battery),"
                " app_version=COALESCE(excluded.app_version,"
                " devices.app_version),"
                " type=COALESCE(excluded.type, devices.type),"
                " online_since=excluded.online_since",
                (imei, _opt_str(body, "phone"),
                 1 if online else 0, now,
                 _opt_str(body, "model"), battery_s,
                 _opt_str(body, "app_version"), _opt_str(body, "type"),
                 online_since))
            conn.commit()
        finally:
            conn.close()
    return 200, {"ok": True}


# ---------- 接口 2：短信上报 ----------
def record_sms(body: dict):
    """POST /api/v1/sms：存一条短信；未知 imei 自动建设备记录。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "请求体不是合法 JSON"}
    imei = _clean_imei(body)
    if not imei:
        return 400, {"ok": False, "error": "imei 必填"}
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return 400, {"ok": False, "error": "content 必填"}
    now = time.time()
    device_ts = _parse_device_ts(body, now)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("INSERT OR IGNORE INTO devices(imei) VALUES(?)",
                         (imei,))
            cur = conn.execute(
                "INSERT INTO sms(imei, sender, content, device_ts, received_at)"
                " VALUES(?,?,?,?,?)",
                (imei, _opt_str(body, "from"), content, device_ts, now))
            conn.execute("DELETE FROM sms WHERE received_at < ?",
                         (now - SMS_TTL,))
            conn.commit()
            rid = cur.lastrowid
        finally:
            conn.close()
    return 200, {"ok": True, "id": rid}


# ---------- 展示数据 ----------
def _device_out(r, now: float) -> dict:
    d = dict(r)
    d["online_now"] = bool(
        d.get("last_seen") and (now - float(d["last_seen"])) <= ONLINE_WINDOW
        and d.get("online_reported"))
    since = d.get("online_since")
    d["online_duration"] = (
        fmt_duration(now - float(since))
        if d["online_now"] and since else "—")
    return d


def list_devices(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY (last_seen IS NULL), last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_device_out(r, now) for r in rows]


def _dev_order(sort: str, order: str, now: float) -> str:
    """设备表排序子句（服务端排序，配合分页）。order 是对该列值的
    升/降序；文本列空值沉底；在线时长按 online_since 排（时长越长
    since 越小），离线设备两个方向都沉底。"""
    d = "ASC" if order == "asc" else "DESC"
    fresh = now - ONLINE_WINDOW
    if sort == "phone":
        return f"ORDER BY (phone IS NULL OR phone=''), phone {d}, imei"
    if sort == "type":
        return f"ORDER BY (type IS NULL OR type=''), type {d}, imei"
    if sort == "status":
        return ("ORDER BY (CASE WHEN online_reported=1"
                f" AND last_seen >= {fresh!r} THEN 1 ELSE 0 END) {d},"
                " last_seen DESC")
    if sort == "duration":
        since_dir = "DESC" if order == "asc" else "ASC"
        return ("ORDER BY (CASE WHEN online_reported=1"
                f" AND last_seen >= {fresh!r}"
                " AND online_since IS NOT NULL THEN 0 ELSE 1 END),"
                f" online_since {since_dir}")
    return f"ORDER BY (last_seen IS NULL), last_seen {d}"


def list_devices_page(page: int = 1, per_page: int = PAGE_SIZE,
                      now: float | None = None,
                      sort: str = "last_seen", order: str = "desc"):
    """设备分页：返回 (本页设备列表, 总数)。页码越界自动夹到有效范围。
    sort ∈ phone/type/status/duration/last_seen（默认最后心跳降序）。"""
    now = time.time() if now is None else now
    if sort not in ("phone", "type", "status", "duration", "last_seen"):
        sort = "last_seen"
    order = "asc" if order == "asc" else "desc"
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, int(page or 1)), pages)
        rows = conn.execute(
            "SELECT * FROM devices " + _dev_order(sort, order, now)
            + " LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)).fetchall()
    finally:
        conn.close()
    return [_device_out(r, now) for r in rows], total


def update_device(imei: str, phone, type_, remark) -> bool:
    """管理页编辑设备：手机号 / 类型 / 备注（IMEI 是身份不可改）。
    空字符串视为清空（存 NULL）；设备不存在返回 False。"""
    imei = (imei or "").strip()
    if not imei:
        return False

    def _norm(v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute(
                "UPDATE devices SET phone=?, type=?, remark=? WHERE imei=?",
                (_norm(phone), _norm(type_), _norm(remark), imei))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def purge_old_sms(now: float | None = None) -> int:
    """清掉超过 SMS_TTL 的短信，返回清掉条数。"""
    now = time.time() if now is None else now
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM sms WHERE received_at < ?",
                               (now - SMS_TTL,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def list_sms(limit: int = 200) -> list[dict]:
    purge_old_sms()
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT s.*, d.phone AS phone FROM sms s"
            " LEFT JOIN devices d ON d.imei = s.imei"
            " ORDER BY s.id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _sms_order(sort: str, order: str) -> str:
    """短信表排序子句。设备列按页面显示值（手机号，无则 IMEI）排；
    时间列以 id 作同值次序键，保持稳定。"""
    d = "ASC" if order == "asc" else "DESC"
    if sort == "device_ts":
        return f"ORDER BY (s.device_ts IS NULL), s.device_ts {d}, s.id {d}"
    if sort == "device":
        return (f"ORDER BY COALESCE(NULLIF(d.phone,''), s.imei) {d},"
                f" s.id {d}")
    if sort == "sender":
        return (f"ORDER BY (s.sender IS NULL OR s.sender=''),"
                f" s.sender {d}, s.id {d}")
    return f"ORDER BY s.received_at {d}, s.id {d}"


def list_sms_page(page: int = 1, per_page: int = PAGE_SIZE,
                  imei: str | None = None, keyword: str | None = None,
                  sort: str = "received", order: str = "desc"):
    """短信分页 + 筛选：imei 精确筛设备，keyword 子串匹配内容/发送方。
    sort ∈ received（上传时间）/device_ts（短信时间）/device/sender，
    默认上传时间降序。返回 (本页短信列表, 总数)；读前先清过期短信。"""
    purge_old_sms()
    if sort not in ("received", "device_ts", "device", "sender"):
        sort = "received"
    order = "asc" if order == "asc" else "desc"
    where, args = [], []
    if imei:
        where.append("s.imei = ?")
        args.append(imei)
    if keyword:
        where.append("(s.content LIKE ? OR s.sender LIKE ?)")
        like = f"%{keyword}%"
        args += [like, like]
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM sms s{wsql}",
                             args).fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, int(page or 1)), pages)
        rows = conn.execute(
            "SELECT s.*, d.phone AS phone FROM sms s"
            " LEFT JOIN devices d ON d.imei = s.imei"
            f"{wsql} " + _sms_order(sort, order) + " LIMIT ? OFFSET ?",
            args + [per_page, (page - 1) * per_page]).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows], total


def delete_sms(sid) -> bool:
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return False
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM sms WHERE id=?", (sid,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def clear_sms() -> int:
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM sms")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---------- 接入请求日志（脱敏） ----------
_log_lock = threading.Lock()
_BJ_TZ = timezone(timedelta(hours=8))  # 日志时间一律北京时间


def log_access(endpoint: str, status: int, body, key_present: bool,
               content_type: str) -> None:
    """给 heartbeat / sms 的每次请求在 fleet_access.log 追加一行：
    北京时间（到秒）\\t 接口 \\t 状态码 \\t IMEI 后 4 位（无记 -）\\t
    key:yes/no \\t Content-Type \\t 请求体字段名列表（逗号分隔，只记
    字段名不记值）。绝不记密钥值、短信内容、完整手机号/完整 IMEI。
    写日志全程 try 包住：失败静默，永不影响接口返回。"""
    try:
        tail, fields = "-", "-"
        if isinstance(body, dict):
            imei = _clean_imei(body)
            if imei:
                tail = imei[-4:]
            names = sorted(str(k) for k in body.keys())
            if names:
                fields = ",".join(names)
        ct = (content_type or "").split(";")[0].strip() or "-"
        ts = datetime.now(_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        line = "\t".join([ts, endpoint, str(int(status)), tail,
                          "key:yes" if key_present else "key:no",
                          ct, fields])
        with _log_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            created = not LOG_FILE.exists()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if created:
                try:
                    os.chmod(LOG_FILE, 0o600)
                except Exception:
                    pass
    except Exception:
        pass


def read_access_log(limit: int = 50) -> list[dict]:
    """读 fleet_access.log 最后 limit 行，解析为 dict 列表（文件顺序：
    旧 → 新，页面展示时再倒序）。坏行跳过；文件不存在返回空列表。"""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-max(1, int(limit)):]:
        parts = ln.split("\t")
        if len(parts) != 7:
            continue
        ts, ep, st, tail, keyf, ct, fields = parts
        try:
            status = int(st)
        except (TypeError, ValueError):
            continue
        out.append({"time": ts, "endpoint": ep, "status": status,
                    "imei_tail": tail, "key": keyf == "key:yes",
                    "content_type": ct, "fields": fields})
    return out
