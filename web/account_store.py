# -*- coding: utf-8 -*-
"""账号库：本地 DPAPI 加密保存多个 CodeBuddy 账号的登录态。

存储格式说明：
  每条账号记录的 `keys` = { secret_key: IDE 会话明文 }，明文结构必须与 CodeBuddy
  写入 state.vscdb 的一致（account / auth / token / accessToken / refreshToken /
  expiresAt / id / domain / converted），否则 IDE 读不出来。
  粘贴进来的"账号面板导出视图"（auth_raw / profile_raw / quota_raw 等）会在导入时
  由 build_ide_session() 统一转换成上述结构。
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path

from cb_secrets import dpapi_protect, dpapi_unprotect

STORE_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
STORE_FILE = STORE_DIR / "accounts.bin"
# 占位 key：粘贴导入/登录得到的账号不绑定具体 secret key，切号时按当前 DB 解析
# （旧版 accessToken / 国内版 accessTokencn 均兼容）。
PLACEHOLDER_KEY = "##current##"

DEFAULT_STORAGE_ID = "Tencent-Cloud.genie-ide-cn"
DEFAULT_DOMAIN = "www.codebuddy.cn"

ACCOUNT_KEYS_TO_DROP = (
    "access_token", "accessToken", "refresh_token", "refreshToken",
    "email", "uid", "nickname", "label", "preferred_username",
)

PAYMENT_LABELS = {
    "free": "免费版", "pro": "专业版", "ultimate": "旗舰版",
    "exclusive": "专属版", "enterprise": "企业版", "team": "团队版",
}


class AccountStore:
    def __init__(self, path: Path | None = None):
        self.path = path or STORE_FILE

    # ---- 存储 ----
    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if not raw:
            return []
        try:
            data = dpapi_unprotect(raw)
            accounts = json.loads(data.decode("utf-8"))
        except Exception:
            return []
        return accounts if isinstance(accounts, list) else []

    def save(self, accounts: list[dict]) -> None:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(accounts, ensure_ascii=False, indent=2).encode("utf-8")
        self.path.write_bytes(dpapi_protect(blob))

    # ---- 查询 ----
    def list_accounts(self) -> list[dict]:
        return self.load()

    def get(self, account_id: str) -> dict | None:
        for a in self.load():
            if a.get("id") == account_id:
                return a
        return None

    # ---- 增删 ----
    def add(self, account: dict) -> str:
        accounts = self.load()
        # 同 uid 覆盖更新，避免重复
        uid = account.get("uid") or account.get("id")
        for i, a in enumerate(accounts):
            if a.get("uid") and a.get("uid") == uid:
                account["id"] = a["id"]
                account["created_at"] = a.get("created_at") or account.get("created_at")
                account["quota"] = account.get("quota") or a.get("quota")
                accounts[i] = account
                self.save(accounts)
                return a["id"]
        account.setdefault("id", uuid.uuid4().hex)
        account.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        accounts.append(account)
        self.save(accounts)
        return account["id"]

    def update(self, account: dict) -> bool:
        accounts = self.load()
        for i, a in enumerate(accounts):
            if a.get("id") == account.get("id"):
                accounts[i] = account
                self.save(accounts)
                return True
        return False

    def remove(self, account_id: str) -> bool:
        accounts = self.load()
        rest = [a for a in accounts if a.get("id") != account_id]
        if len(rest) == len(accounts):
            return False
        self.save(rest)
        return True


# ---------- 工具 ----------
def _jwt_payload(token) -> dict:
    """不校验签名，仅取 JWT 载荷（用于兜底拿 uid/sub）。"""
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _strip_uid_prefix(token) -> str:
    """顶层 accessToken 形如 `uid+jwt`，取 jwt 部分。"""
    if isinstance(token, str) and "+" in token:
        head, _, tail = token.partition("+")
        if tail.startswith("ey") and len(head) >= 8:
            return tail
    return token if isinstance(token, str) else ""


def _merge_auth(*sources) -> dict:
    auth: dict = {}
    for s in sources:
        if isinstance(s, dict):
            for k, v in s.items():
                if v not in (None, ""):
                    auth.setdefault(k, v)
    return auth


# ---------- 核心：规整为 IDE 会话明文 ----------
def build_ide_session(obj, ref_id: str | None = None) -> str | None:
    """把任意登录态视图转换成 IDE state.vscdb 中存储的会话明文 JSON。

    支持输入：
      1. 已是 IDE 会话明文（顶层含 account + auth）；
      2. 账号面板导出视图（含 auth_raw / profile_raw / quota_raw / access_token ...）；
      3. 松散 token 视图（access_token / refresh_token / accessToken ...）。
    返回 None 表示无法识别。
    """
    if not isinstance(obj, dict):
        return None

    auth_src = obj.get("auth") if isinstance(obj.get("auth"), dict) else None
    auth_raw = obj.get("auth_raw") if isinstance(obj.get("auth_raw"), dict) else None
    profile = obj.get("profile_raw") if isinstance(obj.get("profile_raw"), dict) else None
    account_src = obj.get("account") if isinstance(obj.get("account"), dict) else None

    access = _strip_uid_prefix(obj.get("access_token") or obj.get("accessToken") or "")
    refresh = obj.get("refresh_token") or obj.get("refreshToken") or ""
    auth = _merge_auth(auth_src, auth_raw)
    if auth.get("accessToken"):
        access = access or _strip_uid_prefix(auth["accessToken"])
    if auth.get("refreshToken"):
        refresh = refresh or auth["refreshToken"]
    access = _strip_uid_prefix(access)
    if not access:
        return None

    uid = ""
    for src in (account_src, profile, obj):
        if isinstance(src, dict) and not uid:
            uid = str(src.get("uid") or "")
    uid = uid or str(_jwt_payload(access).get("sub") or "")

    name = ""
    for src in (account_src, profile, obj):
        if isinstance(src, dict) and not name:
            name = str(src.get("label") or src.get("nickname") or "")
    if not name and isinstance(profile, dict):
        name = str(profile.get("phoneNumber") or "")
    if not name:
        name = str(obj.get("nickname") or obj.get("preferred_username")
                   or obj.get("email") or "")
    name = name or uid

    auth_out = dict(auth)
    auth_out["accessToken"] = access
    if refresh:
        auth_out["refreshToken"] = refresh
    auth_out["domain"] = (auth.get("domain") or obj.get("domain")
                          or (profile or {}).get("domain") or DEFAULT_DOMAIN)

    account_out = dict(account_src or {})
    account_out["id"] = account_out.get("id") or uid
    account_out["uid"] = uid
    account_out["label"] = account_out.get("label") or name
    account_out["nickname"] = account_out.get("nickname") or name
    account_out.setdefault("enterpriseId", (profile or {}).get("enterpriseId", ""))
    account_out.setdefault("enterpriseName", "")
    account_out.setdefault("pluginEnabled", True)
    account_out.setdefault("lastLogin", True)
    if isinstance(profile, dict):
        for k in ("uin", "type", "phoneNumber", "accountType"):
            if profile.get(k) not in (None, ""):
                account_out.setdefault(k, profile[k])

    now_ms = int(time.time() * 1000)
    drop = {"account", "auth", "auth_raw", "profile_raw", "quota_raw", "payment_type",
            "dosage_notify_code", "access_token", "refresh_token", "token_type",
            "status", "checkin_streak", "quota_query_last_error",
            "quota_query_last_error_at", "last_used", "id", "accessToken",
            "refreshToken", "token", "expiresAt", "refreshExpiresAt", "converted",
            "email", "uid", "nickname", "created_at", "last_used", "label"}
    session = {k: v for k, v in obj.items()
               if k not in drop and not k.startswith("_")}

    src_id = obj.get("id")
    session["id"] = (ref_id or (src_id if isinstance(src_id, str)
                                and src_id.startswith("Tencent-Cloud") else "")
                     or DEFAULT_STORAGE_ID)
    session["domain"] = obj.get("domain") or auth_out["domain"] or DEFAULT_DOMAIN
    session["converted"] = True
    session["account"] = account_out
    session["auth"] = auth_out
    session["accessToken"] = f"{uid}+{access}" if uid else access
    session["refreshToken"] = refresh or auth_out.get("refreshToken", "")
    session["token"] = access

    expires_at = int(auth_out.get("expiresAt") or 0)
    if not expires_at and auth_out.get("expiresIn"):
        expires_at = now_ms + int(auth_out["expiresIn"]) * 1000
    session["expiresAt"] = expires_at
    refresh_expires_at = int(auth_out.get("refreshExpiresAt") or 0)
    if not refresh_expires_at and auth_out.get("refreshExpiresIn"):
        refresh_expires_at = now_ms + int(auth_out["refreshExpiresIn"]) * 1000
    if refresh_expires_at:
        session["refreshExpiresAt"] = refresh_expires_at

    ordered = {}
    for k in ("id", "domain", "converted", "account", "accounts", "auth",
              "accessToken", "refreshToken", "token", "expiresAt", "refreshExpiresAt"):
        if k in session:
            ordered[k] = session.pop(k)
    ordered.update(session)
    return json.dumps(ordered, ensure_ascii=False)


# ---------- 额度 ----------
def extract_quota(obj) -> dict | None:
    """从账号记录/导出视图中提取已缓存的额度信息。"""
    if not isinstance(obj, dict):
        return None
    quota_raw = obj.get("quota_raw") if isinstance(obj.get("quota_raw"), dict) else None
    payment = obj.get("payment_type") or ""
    notify = obj.get("dosage_notify_code")
    if quota_raw:
        payment = payment or ((quota_raw.get("payment") or {}).get("data") or {}).get("paymentType") or ""
        dosage = (quota_raw.get("dosage") or {}).get("data") or {}
        notify = dosage.get("dosageNotifyCode", notify)
    if not payment and not notify:
        return None
    return {
        "payment_type": payment,
        "total_remain": None,
        "total_size": None,
        "dosage_notify_code": notify,
        "source": "record",
    }


def _fmt_num(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "-"
    return str(int(f)) if f.is_integer() else f"{f:g}"


def quota_columns(record: dict) -> tuple[str, str]:
    """返回 (套餐, 额度) 两列的显示文本。

    套餐 = payment_type（get-payment-type，与 IDE 一致：free/pro/ultimate/exclusive）；
    额度 = usage_left/usage_total/usage_used（统计口径见 cb_api.fetch_quota），
    兼容旧缓存字段 total_remain/total_size。
    """
    q = record.get("quota") or {}
    if not isinstance(q, dict):
        return "-", "-"
    payment = q.get("payment_type") or ""
    plan = PAYMENT_LABELS.get(payment, payment or "-")
    if q.get("error") and q.get("usage_left") is None:
        return plan, f"查询失败"
    if not plan and q.get("plan_name"):
        plan = str(q["plan_name"])[:20]

    left, total, used = q.get("usage_left"), q.get("usage_total"), q.get("usage_used")
    if left is None and total is None:
        # 兼容旧缓存
        left, total = q.get("total_remain"), q.get("total_size")
    if left is None and total is None:
        code = q.get("dosage_notify_code")
        if code is not None and str(code) != "0":
            return plan, f"额度告警({code})"
        if q.get("error"):
            return plan, f"查询失败"
        return plan, "-"
    if left is None:
        text = _fmt_num(total)
    else:
        text = f"{_fmt_num(left)}/{_fmt_num(total)}"
    if used and str(used) not in ("0", "0.0"):
        text += f"（已用 {_fmt_num(used)}）"
    # 到期日：专业/付费订阅显示扣费到期；免费/试用显示额度周期刷新日（更贴近实际）
    d = q.get("expire_at") or ""
    if d and not q.get("is_pro"):
        d = q.get("refresh_at") or d
    if d:
        text += f"· {str(d)[:10]}"
    if q.get("error"):
        text = "查询失败"
    return plan, text


# ---------- 解析/构造 ----------
def _pick_meta(obj: dict) -> dict:
    """从账号记录外层/内层提取展示信息。"""
    profile = obj.get("profile_raw")
    profile = profile if isinstance(profile, dict) else {}
    acc = obj.get("account") or (obj.get("auth_raw") or {}).get("account") or {}
    acc = acc if isinstance(acc, dict) else {}
    meta = {}
    for field in ("uid", "email", "nickname", "label"):
        v = obj.get(field) or acc.get(field) or profile.get(field) \
            or profile.get("phoneNumber") or profile.get("uid")
        if v:
            meta[field] = str(v)
    if "label" not in meta:
        meta["label"] = (obj.get("nickname") or acc.get("label")
                         or profile.get("nickname") or profile.get("phoneNumber")
                         or meta.get("email") or "")
    if not meta.get("email") and profile.get("phoneNumber"):
        meta["email"] = str(profile["phoneNumber"])
    return meta


def summarize_account(keys: dict[str, str], source_label: str = "",
                      meta: dict | None = None) -> dict:
    """keys: {secret_key: IDE 会话明文} -> 汇总成一个账号记录。

    注意：明文顶层 id 是客户端标识（Tencent-Cloud.genie-ide-cn），不是账号 uid；
    真实 uid 在 account.uid / profile_raw.uid / 外层 meta.uid。"""
    uid, email, nickname, label = "", "", "", ""
    for plain in keys.values():
        try:
            obj = json.loads(plain)
        except Exception:
            obj = {}
        if isinstance(obj, dict):
            acc = obj.get("account") or {}
            if not isinstance(acc, dict):
                acc = {}
            profile = obj.get("profile_raw")
            profile = profile if isinstance(profile, dict) else {}
            uid = uid or obj.get("uid") or acc.get("uid") or profile.get("uid") or ""
            email = email or obj.get("email") or acc.get("email") or ""
            nickname = (nickname or obj.get("nickname") or acc.get("nickname")
                        or profile.get("nickname") or profile.get("phoneNumber") or "")
            label = (label or obj.get("label") or acc.get("label")
                     or obj.get("preferred_username") or nickname or email or "")
        elif isinstance(obj, list) and obj:
            o = obj[0] if isinstance(obj[0], dict) else {}
            acc = o.get("account") or {}
            acc = acc if isinstance(acc, dict) else {}
            uid = uid or o.get("uid") or acc.get("uid") or ""
            email = email or o.get("email") or acc.get("email") or ""
            nickname = nickname or o.get("nickname") or acc.get("nickname") or ""
            label = label or o.get("label") or acc.get("label") or nickname or email or ""
    if meta:
        uid = uid or meta.get("uid", "")
        email = email or meta.get("email", "")
        nickname = nickname or meta.get("nickname", "")
        label = label or meta.get("label", "")
    uid = uid or source_label
    return {
        "id": uid or uuid.uuid4().hex,
        "uid": uid,
        "email": email,
        "label": label or nickname or email or uid or "未命名账号",
        "keys": keys,
        "source": source_label,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _to_store_plain(obj) -> tuple[str | None, dict]:
    """把单个账号对象规整为"写回 state.vscdb 用的 IDE 会话明文"。
    返回 (存储明文, 外层摘要 meta)；无法识别返回 (None, {})。"""
    plain = build_ide_session(obj)
    if not plain:
        return None, {}
    return plain, _pick_meta(obj)


def _extract_json_values(text: str) -> list:
    """从文本中循环提取所有完整 JSON 值（支持多段 JSON、代码块、前后说明文字等）。"""
    dec = json.JSONDecoder()
    out: list = []
    idx, n = 0, len(text)
    guard = 0
    while idx < n and guard < 200:
        guard += 1
        # 跳过空白与非 JSON 前导，找到下一个 { 或 [
        while idx < n and text[idx] in " \t\r\n`\ufeff":
            idx += 1
        if idx >= n:
            break
        if text[idx] not in "[{":
            nxt = n
            for ch in ("[", "{"):
                p = text.find(ch, idx)
                if p != -1 and p < nxt:
                    nxt = p
            if nxt >= n:
                break
            idx = nxt
            continue
        try:
            obj, end = dec.raw_decode(text, idx)
        except json.JSONDecodeError:
            # 某段残缺则跳过其起始字符继续找
            idx += 1
            continue
        out.append(obj)
        idx = end
    return out


def parse_pasted_text(text: str) -> list[dict]:
    """解析用户粘贴的登录态，支持对象或数组、一条或多条、一段或多段 JSON。
    返回账号 dict 列表。"""
    values = _extract_json_values(text)
    objs: list[dict] = []
    for v in values:
        if isinstance(v, dict):
            objs.append(v)
        elif isinstance(v, list):
            objs.extend(x for x in v if isinstance(x, dict))

    accounts: list[dict] = []
    for obj in objs:
        plain, meta = _to_store_plain(obj)
        if not plain:
            continue
        rec = summarize_account({PLACEHOLDER_KEY: plain}, "粘贴导入", meta)
        quota = extract_quota(obj)
        if quota:
            rec["quota"] = quota
        accounts.append(rec)
    if not accounts:
        raise ValueError("内容中未识别出账号登录态（需要包含 auth_raw / access_token / "
                         "account+auth 等字段）")
    return accounts
