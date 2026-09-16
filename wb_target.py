# -*- coding: utf-8 -*-
"""WorkBuddy 登录态同步。

本机实测（2026-09-16，WorkBuddy 桌面版 5.5.3）——WorkBuddy 有两条互不相干的路径：

1) **现役：桌面版自己存登录态**（明文 JSON），路径规则来自它的 FileSystemPathService：

       <basePath>\\Data\\Public\\auth\\<authenticationId>.info
       = %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info

   内容：`{account, auth{accessToken, refreshToken, expiresAt, domain, ...}, accounts, allAccounts}`。
   客户端每次请求会把 `auth.domain` 放进 `X-Domain` 头。

2) **legacy：WorkBuddy IDE 时代的库**（只在启动时读一次做迁移，之后不再读）：

       %APPDATA%\\WorkBuddy\\User\\globalStorage\\state.vscdb
       key = `planning-genie.new.accessTokencn`（DPAPI + AES-GCM，与 CodeBuddy 相同算法）

   本机该目录为空 → 早已迁移到 1)。

⚠️ `%APPDATA%\\WorkDaddy\\workbuddy-target.json` 描述的是 WorkDaddy 启动器要拉起的 CodeBuddy
IDE（binary / dataRoot / sessionDb），**不是 WorkBuddy 自己的登录态**。旧版工具把它当同步目标，
结果写进了 CodeBuddy CN 的 state.vscdb —— 这就是"切号对 WorkBuddy 没效果"的根因。

写入流程：先用账号的 refreshToken 在 plugin 续期口换新 token（账号库里存的旧 accessToken 在开放
网关上会被 401；实测续期后的 token 在签到 / 成长计划 / 账号列表接口全部 200），再填 auth、
合并 account 元数据，备份原文件后原子写入。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import cb_api
import cb_secrets as cs

AUTH_ID = "workbuddy-desktop"
AUTH_DIR = (Path(os.environ.get("LOCALAPPDATA", ""))
            / "CodeBuddyExtension" / "Data" / "Public" / "auth")
STORE_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
BACKUP_DIR = STORE_DIR / "wb_backup"

# 客户端把自己的 API 基址放进 X-Domain；原会话里就是它（可用 CBAC_WB_DOMAIN 覆盖以便试验）
DEFAULT_DOMAIN = "copilot.tencent.com"

# ---- legacy（旧 WorkBuddy IDE / WorkDaddy target）----
LEGACY_TARGET_FILE = (Path(os.environ.get("APPDATA", ""))
                      / "WorkDaddy" / "workbuddy-target.json")

_ACCOUNT_DEFAULTS: dict = {
    "type": "personal", "lastLogin": True, "isCreator": False, "isAdmin": False,
    "pluginEnabled": True, "accountType": "", "idp": "", "areaInfoComplete": False,
    "oneidAccountId": "", "isCurrentOneIdEnterprise": False,
    "isCurrentOneIdPersonal": False, "isFirstLogin": False,
    "deployStatus": {"statusCode": 0, "statusMsg": "", "detailMsg": ""},
    "sso": {"domain": "", "domainModifiedTimes": 0},
}
_MERGE_KEYS = ("nickname", "uin", "phoneNumber", "type", "accountType", "idp",
               "oneidAccountId", "email")


# ---------- 定位 ----------
def auth_file() -> Path | None:
    """WorkBuddy 桌面版的登录态文件；不存在返回 None（说明装的是旧版）。"""
    exact = AUTH_DIR / f"{AUTH_ID}.info"
    if exact.exists():
        return exact
    if not AUTH_DIR.exists():
        return None
    for cand in sorted(AUTH_DIR.glob("*.info")):
        obj = _read_json(cand)
        if isinstance(obj, dict) and obj.get("auth") and obj.get("account"):
            return cand
    return None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def load_info() -> dict | None:
    """读现在生效的那份登录态（新版 .info 优先，旧版回退 state.vscdb）。"""
    path = auth_file()
    if path is None:
        return None
    return _read_json(path)


# ---------- 读取 ----------
def current_account() -> str | None:
    """当前生效的账号名（新版读 .info，旧版解 state.vscdb）。"""
    info = load_info()
    if isinstance(info, dict):
        acc = info.get("account") or {}
        return acc.get("nickname") or acc.get("label") or acc.get("uid")
    return _legacy_current_account()


def status_line() -> str:
    """给界面用的一行状态说明。"""
    path = auth_file()
    if path is not None:
        who = current_account() or "未知"
        return f"WorkBuddy：{path.name} · 当前账号 {who} · {path.parent}"
    root, db, err, origin = _legacy_resolve()
    if err:
        return f"WorkBuddy：{err}"
    return f"WorkBuddy（旧版）：数据目录 {root}（{origin}）· 登录库 {db.name}"


# ---------- 写入 ----------
def _build_account(uid: str, src: dict, extra: dict | None,
                   template: dict | None) -> dict:
    """账号元数据：默认值 → 原 .info 模板（同 uid 时）→ 账号库里的字段。"""
    acc: dict = json.loads(json.dumps(_ACCOUNT_DEFAULTS))
    if isinstance(template, dict) and template.get("uid") == uid:
        acc.update(template)
    for key in _MERGE_KEYS:
        if src.get(key):
            acc[key] = src[key]
    if extra:
        for key in _MERGE_KEYS:
            if extra.get(key):
                acc[key] = extra[key]
    acc["uid"] = uid
    acc["lastLogin"] = True
    return acc


def _build_auth(new_auth: dict, template_auth: dict | None) -> dict:
    """auth 段：令牌取续期结果，domain 沿用原会话（客户端拿它当 X-Domain）。"""
    now_ms = int(time.time() * 1000)
    domain = ""
    if isinstance(template_auth, dict):
        domain = str(template_auth.get("domain") or "")
    domain = domain or os.environ.get("CBAC_WB_DOMAIN", "").strip() or DEFAULT_DOMAIN
    return {
        "accessToken": new_auth.get("accessToken", ""),
        "expiresIn": int(new_auth.get("expiresIn") or 0),
        "refreshExpiresIn": int(new_auth.get("refreshExpiresIn") or 0),
        "refreshToken": new_auth.get("refreshToken", ""),
        "tokenType": new_auth.get("tokenType") or "Bearer",
        "notBeforePolicy": int(new_auth.get("notBeforePolicy") or 0),
        "sessionState": new_auth.get("sessionState") or "",
        "scope": new_auth.get("scope") or "",
        "domain": domain,
        "lastRefreshTime": int(new_auth.get("lastRefreshTime") or now_ms),
        "expiresAt": int(new_auth.get("expiresAt") or 0),
        "refreshExpiresAt": int(new_auth.get("refreshExpiresAt") or 0),
    }


def sync_session(plain: str | dict, *, extra_account: dict | None = None,
                 refresh: bool = True,
                 backup: bool = True) -> tuple[bool, str, dict | None]:
    """把一条会话明文（JSON 文本或已解析的 dict）写进 WorkBuddy 登录态文件。

    返回 (是否成功, 说明, 续期得到的新 auth 或 None)——新 auth 供调用方写回账号库。
    """
    path = auth_file()
    if path is None:
        ok, msg = _legacy_sync(plain if isinstance(plain, str) else json.dumps(plain))
        return ok, msg, None

    if isinstance(plain, dict):
        src = plain
    else:
        try:
            src = json.loads(plain)
        except Exception as e:  # noqa: BLE001
            return False, f"登录态解析失败：{e}", None

    src_acc = src.get("account") or {}
    uid = str(src_acc.get("uid") or (extra_account or {}).get("uid") or "")
    if not uid:
        return False, "该账号缺少 uid，无法同步到 WorkBuddy", None

    if refresh:
        if not src.get("refreshToken"):
            return False, "该账号没有 refreshToken，无法换取 WorkBuddy 侧 token（请先续期该账号）", None
        try:
            new_auth = cb_api.refresh_auth(src)
        except Exception as e:  # noqa: BLE001
            return False, f"续期失败，未写入 WorkBuddy：{e}", None
    else:
        new_auth = src

    template = _read_json(path) or {}
    account = _build_account(uid, src_acc, extra_account, template.get("account"))
    info = {
        "account": account,
        "auth": _build_auth(new_auth, template.get("auth")),
        "accounts": [account],
        "allAccounts": [account],
    }

    bak = None
    try:
        if backup and path.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            bak = BACKUP_DIR / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, bak)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        return False, f"写入失败：{e}", None

    who = account.get("nickname") or uid
    msg = f"已写入 WorkBuddy 登录态：{who}（{path.name}）"
    if bak:
        msg += f"，原登录态已备份 {bak.name}"
    try:
        import cb_runtime
        if cb_runtime.is_workbuddy_running():
            msg += "；WorkBuddy 正在运行，建议重启它让新账号生效"
    except Exception:  # noqa: BLE001
        pass
    return True, msg, new_auth


# ---------- legacy（旧版 WorkBuddy IDE 的 state.vscdb）----------
def load_target() -> dict | None:
    """读 WorkDaddy 的 target 配置（只有旧版才需要）。"""
    return _read_json(LEGACY_TARGET_FILE)


def _legacy_candidates(t: dict) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    root = t.get("dataRoot")
    if root:
        out.append((Path(root), "target.dataRoot"))
    roam = Path(os.environ.get("APPDATA", ""))
    out.append((roam / "WorkBuddy", "WorkBuddy(旧) 数据目录"))
    out.append((roam / "WorkDaddy", "WorkDaddy 数据目录"))
    return out


def _legacy_resolve() -> tuple[Path | None, Path | None, str, str]:
    """解析旧版 (root, state.vscdb, 错误说明, 来源说明)。"""
    t = load_target()
    candidates = _legacy_candidates(t or {})
    tried: list[str] = []
    for path, origin in candidates:
        if not path.exists():
            continue
        db = cs.find_state_db(path)
        if db:
            return path, db, "", origin
        tried.append(str(path))
    if not t:
        return None, None, (f"未找到 WorkBuddy 登录态（新版 {AUTH_DIR} 无 .info，"
                            f"旧版 {LEGACY_TARGET_FILE} 也不存在）"), ""
    return (None, None,
            "未找到 WorkBuddy 旧版数据目录（" + (", ".join(tried) or "无候选") + "）", "")


def _legacy_current_account() -> str | None:
    _, db, err, _ = _legacy_resolve()
    if err or db is None:
        return None
    try:
        aes = cs.aes_key_from(db.parent / "Local State")
        got = cs.read_current_account(db, aes)
    except Exception:  # noqa: BLE001
        return None
    if not got:
        return None
    try:
        obj = json.loads(got[1])
    except Exception:  # noqa: BLE001
        return "（未知账号）"
    acc = obj.get("account") if isinstance(obj, dict) else None
    if isinstance(acc, dict):
        return acc.get("label") or acc.get("nickname") or acc.get("uid")
    if isinstance(obj, dict):
        return obj.get("label") or obj.get("nickname") or obj.get("uid")
    return "（未知账号）"


def _legacy_sync(plain: str) -> tuple[bool, str]:
    """旧版：把会话明文加密写进 WorkBuddy 的 state.vscdb。"""
    root, db, err, origin = _legacy_resolve()
    if err or db is None:
        return False, err or "WorkBuddy 登录态不可用"
    ls = root / "Local State"
    try:
        aes = cs.aes_key_from(ls)
        keys = cs.resolve_target_keys(db)
        entries = {k: cs.encrypt_v10(plain, aes) for k in keys}
        bak = cs.backup_db(db)
        n = cs.write_secrets(entries, db=db)
    except Exception as e:  # noqa: BLE001
        return False, f"同步失败：{e}"
    msg = f"已写入 {n} 条登录态到 {db.name}（{origin}）"
    if bak:
        msg += f"，原登录态已备份 {Path(bak).name}"
    return True, msg
