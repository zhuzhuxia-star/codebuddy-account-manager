# -*- coding: utf-8 -*-
"""核心业务流程：导入当前 IDE 账号 / 一键切号 / 软件内登录 / 额度查询 / 续期。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cb_api
import cb_cloud
import cb_log
import cb_runtime
import cb_secrets as cs
import wb_api
import wb_target
from account_store import (DEFAULT_STORAGE_ID, AccountStore, build_ide_session,
                           quota_columns, summarize_account)


class SwitchResult:
    def __init__(self, ok: bool, message: str, backup: str | None = None):
        self.ok = ok
        self.message = message
        self.backup = backup


def _normalize_plain(plain: str, ref_id: str | None) -> str:
    """把要写回的会话明文规整为与当前 IDE 一致：
    顶层 id 对齐当前存储客户端（旧版 genie-ide -> 国内版 genie-ide-cn），
    并补齐 converted / domain 等字段。"""
    try:
        obj = json.loads(plain)
    except Exception:
        return plain
    if not isinstance(obj, dict):
        return plain
    return build_ide_session(obj, ref_id) or plain


def record_auth(record: dict) -> dict | None:
    """取出账号记录里可用的 auth（accessToken/refreshToken/domain）。"""
    from account_store import _strip_uid_prefix
    for plain in (record.get("keys") or {}).values():
        try:
            obj = json.loads(plain)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        auth = obj.get("auth")
        if isinstance(auth, dict) and auth.get("accessToken"):
            return auth
        if obj.get("accessToken"):
            return {
                "accessToken": _strip_uid_prefix(obj.get("accessToken")),
                "refreshToken": obj.get("refreshToken") or "",
                "domain": obj.get("domain") or "",
            }
    return None


def import_from_ide(store: AccountStore, db: Path | None = None) -> dict:
    """从当前 CodeBuddy 的 state.vscdb 导入该扩展账号（解密为明文）。"""
    from account_store import summarize_account
    entries = cs.find_login_entries(db)  # 只取登录态条目（识别 key 名/内容）
    if not entries:
        raise RuntimeError("未在 IDE 登录态中发现账号条目。\n"
                           "请确认已在 CodeBuddy 中登录过账号，且工具定位到的数据目录正确。")
    return summarize_account(entries, source_label="从 IDE 导入")


def switch_to_account(account: dict, force_kill: bool = False,
                      sync_workbuddy: bool = True) -> SwitchResult:
    """切号：备份 DB -> 用目标账号明文加密写回 -> 同步到 WorkBuddy。返回结果。"""
    keys = account.get("keys") or {}
    if not keys:
        return SwitchResult(False, "该账号没有可用的登录数据")

    # 占位 key（粘贴导入/软件登录的账号）在切号前解析为 IDE 实际读取的 key 名。
    # 注意：旧版/国内版 key 名不同（accessToken / accessTokencn），必须全部覆盖写入，
    # 否则会出现"写进去了但 IDE 读的还是旧账号"。
    placeholder = any(k.startswith("##") for k in keys)
    target_keys: list[str] = []
    if placeholder:
        try:
            target_keys = cs.resolve_target_keys()
        except Exception as e:
            return SwitchResult(False, f"解析登录条目 key 失败：{e}")

    if cb_runtime.is_running() or cb_runtime.is_workbuddy_running():
        if not force_kill:
            return SwitchResult(False, "CodeBuddy / WorkBuddy 正在运行，请先退出后再切号")
        cb_runtime.terminate()
        time.sleep(1.5)

    try:
        aes = cs.get_aes_key()
        ref_id = cs.get_current_storage_id() or DEFAULT_STORAGE_ID
        entries: dict[str, bytes] = {}
        plains: list[str] = []
        for k, p in keys.items():
            real_keys = target_keys if k.startswith("##") else [k]
            plain = _normalize_plain(p, ref_id)
            plains.append(plain)
            for rk in real_keys:
                entries[rk] = cs.encrypt_v10(plain, aes)
        bak = cs.backup_db()
        n = cs.write_secrets(entries)
    except Exception as e:
        return SwitchResult(False, f"切号失败: {e}")

    # 写回校验：重新解密，确认 IDE 读到的确实是目标账号
    verified, actual = verify_current(account.get("uid") or "")

    msg = f"已切换到账号：{account.get('label')}（写入 {n} 条登录态条目）"
    msg += f"\n写入 key：\n  " + "\n  ".join(sorted(entries))
    if bak:
        msg += f"\n原登录态已备份：{Path(bak).name}"
    msg += f"\n校验：{'通过，IDE 下次启动即为该账号' if verified else '未通过，当前读到的是 ' + (actual or '空')}"

    # 同步到 WorkBuddy（同一套 DPAPI + AES-GCM，写入 target 声明的数据目录）
    if sync_workbuddy and plains:
        ok, wb_msg = wb_target.sync_session(plains[0])
        msg += f"\nWorkBuddy 同步：{'成功 — ' if ok else '跳过 — '}{wb_msg}"
        cb_log.log(f"WorkBuddy 同步 | {'OK' if ok else 'SKIP'} | {wb_msg}", "switch")
    cb_log.log(f"切号 | {account.get('label')} | 写入 {n} 条 | 校验="
               f"{'通过' if verified else '未通过'}", "switch")
    return SwitchResult(True, msg, str(bak) if bak else None)


def sync_to_workbuddy(account: dict) -> tuple[bool, str]:
    """把某个账号的登录态单独同步到 WorkBuddy。"""
    if cb_runtime.is_running() or cb_runtime.is_workbuddy_running():
        return False, "请先退出 CodeBuddy / WorkBuddy 再同步（state.vscdb 被占用时无法写入）"
    plains = list((account.get("keys") or {}).values())
    if not plains:
        return False, "该账号没有可用的登录数据"
    ref_id = None
    try:
        ref_id = cs.get_current_storage_id()
    except Exception:
        pass
    return wb_target.sync_session(_normalize_plain(plains[0], ref_id))


def verify_current(uid: str) -> tuple[bool, str]:
    """读回 IDE 当前登录态，判断是否已是指定 uid。"""
    try:
        got = cs.read_current_account()
        if not got:
            return False, ""
        key, plain = got
        obj = json.loads(plain)
        acc = (obj.get("account") or {}) if isinstance(obj, dict) else {}
        actual_uid = acc.get("uid") or ""
        actual_name = acc.get("label") or acc.get("nickname") or actual_uid or "?"
        return (bool(uid) and actual_uid == uid, f"{actual_name}（{key.split(chr(34))[-2]}）")
    except Exception:
        return False, ""


# ---------- 软件内登录 ----------
def login_account(on_auth_url=None, on_status=None, should_stop=None,
                  endpoint: str | None = None) -> dict:
    """走官方 external-link 登录流程，返回可入库的账号记录。

    on_auth_url(url): 拿到登录地址时回调（GUI 负责打开浏览器）；
    on_status(text): 进度提示；should_stop(): 取消信号。
    """
    ep = endpoint or cb_api.resolve_endpoint()
    if on_status:
        on_status(f"连接 {ep} …")
    info = cb_api.fetch_auth_state(ep)
    url, state = info["authUrl"], info["state"]
    if on_auth_url:
        on_auth_url(url)
    if on_status:
        on_status("已在浏览器打开登录页，请完成登录…")
    auth = cb_api.poll_auth_token(state, should_stop=should_stop, endpoint=ep)
    if on_status:
        on_status("登录成功，正在获取账号信息…")
    account = cb_api.poll_login_account(state, auth, should_stop=should_stop, endpoint=ep)
    accounts = cb_api.fetch_accounts(auth, ep)

    ref_id = None
    try:
        ref_id = cs.get_current_storage_id()
    except Exception:
        pass
    plain = build_ide_session(
        {"account": account, "auth": auth, "accounts": accounts,
         "domain": auth.get("domain") or ""},
        ref_id=ref_id or DEFAULT_STORAGE_ID)
    if not plain:
        raise RuntimeError("登录返回的数据无法转换为 IDE 登录态")
    rec = summarize_account({"##current##": plain}, source_label="软件登录")
    rec["accounts"] = accounts
    try:
        rec["quota"] = cb_api.fetch_quota(auth, ep)
    except Exception:
        pass
    return rec


# ---------- 额度 / 续期 ----------
def _now() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


def _rebuild_keys_with_auth(rec: dict, new_auth: dict) -> dict:
    """把新 auth（续期后）合并回账号记录里的每份会话明文。"""
    ref_id = None
    try:
        ref_id = cs.get_current_storage_id()
    except Exception:
        pass
    keys = {}
    for k, plain in (rec.get("keys") or {}).items():
        try:
            obj = json.loads(plain)
        except Exception:
            keys[k] = plain
            continue
        if isinstance(obj, dict):
            merged = dict(obj)
            merged["auth"] = {**(obj.get("auth") or {}), **new_auth}
            merged["domain"] = new_auth.get("domain") or obj.get("domain") or ""
            keys[k] = build_ide_session(merged, ref_id) or plain
        else:
            keys[k] = plain
    rec["keys"] = keys
    return rec


def refresh_quota(store: AccountStore, account_id: str) -> tuple[bool, str]:
    """联网刷新某个账号的额度，写回账号库。返回 (是否成功, 展示文本)。

    access token 失效（过期/被顶号）时会先自动用 refresh token 续期再查一次。
    """
    rec = store.get(account_id)
    if not rec:
        return False, "账号不存在"
    auth = record_auth(rec)
    if not auth:
        return False, "该账号没有可用的 access token"

    quota = cb_api.fetch_quota(auth)
    if quota.get("error") and auth.get("refreshToken"):
        # token 失效：续期后重试一次
        try:
            new_auth = cb_api.refresh_auth(auth)
        except Exception:
            pass
        else:
            try:
                _rebuild_keys_with_auth(rec, new_auth)
                rec["refreshed_at"] = _now()
            except Exception:
                pass
            quota = cb_api.fetch_quota(new_auth)

    quota["updated_at"] = _now()
    rec["quota"] = quota
    store.update(rec)
    plan, text = quota_columns(rec)
    if quota.get("error"):
        cb_log.log(f"额度 | {rec.get('label')} | FAIL | {plan} {text} | "
                   f"{quota['error'][:120]}", "quota")
        return False, f"{plan} | {text}（{quota['error'][:120]}）"
    cb_log.log(f"额度 | {rec.get('label')} | OK | {plan} | {text}", "quota")
    return True, f"{plan} | {text}"


def refresh_session(store: AccountStore, account_id: str) -> SwitchResult:
    """用 refresh token 续期账号登录态（不写 IDE，仅更新账号库）。"""
    rec = store.get(account_id)
    if not rec:
        return SwitchResult(False, "账号不存在")
    auth = record_auth(rec)
    if not auth:
        return SwitchResult(False, "该账号没有可用的登录数据")
    try:
        new_auth = cb_api.refresh_auth(auth)
    except Exception as e:
        return SwitchResult(False, f"续期失败：{e}")

    try:
        _rebuild_keys_with_auth(rec, new_auth)
    except Exception as e:
        return SwitchResult(False, f"更新登录态失败：{e}")
    rec["refreshed_at"] = _now()
    store.update(rec)
    return SwitchResult(True, f"已续期：{rec.get('label')}（新 access token 已保存）")


# ---------- 云端保险库（跨电脑同步账号） ----------
def cloud_status() -> str:
    """云端同步状态一行说明。"""
    conf = cb_cloud.load_conf()
    if not conf.get("slug"):
        return f"云端：未建立（首次上传后固定为 {cb_cloud.vault_url()}）"
    saved = "口令已记住" if conf.get("pass_enc") else "口令未保存"
    return (f"云端：{cb_cloud.vault_url()} · {conf.get('accounts', '?')} 个账号 · "
            f"更新于 {conf.get('updated_at', '?')} · {saved}")


def cloud_push(store: AccountStore, passphrase: str) -> tuple[bool, str]:
    """把本地账号库加密上传到云端（跨电脑保存）。"""
    if not passphrase:
        return False, "请先设置同步口令（用于加密，务必牢记：换电脑要用同一个口令）"
    ok, msg = cb_cloud.push(store.list_accounts(), passphrase)
    if ok:
        cb_cloud.save_passphrase(passphrase)
    cb_log.log(f"云端上传 | {'OK' if ok else 'FAIL'} | {msg.splitlines()[0]}", "cloud")
    return ok, msg


def cloud_pull(store: AccountStore, passphrase: str, ref: str | None = None,
               merge: bool = True) -> tuple[bool, str]:
    """从云端取回账号库并合并到本地。"""
    if not passphrase:
        return False, "请输入同步口令"
    ok, res = cb_cloud.pull(passphrase, ref)
    if not ok:
        cb_log.log(f"云端下载 | FAIL | {res}", "cloud")
        return False, str(res)
    remote = res
    if merge:
        merged, added, updated = cb_cloud.merge_accounts(store.list_accounts(), remote)
        store.save(merged)
        msg = f"已从云端同步：新增 {added} 个、更新 {updated} 个（本地共 {len(merged)} 个）"
    else:
        store.save(remote)
        msg = f"已用云端账号库覆盖本地（{len(remote)} 个账号）"
    cb_cloud.save_passphrase(passphrase)
    cb_log.log(f"云端下载 | OK | {msg}", "cloud")
    return True, msg


def cloud_auto_pull(store: AccountStore) -> tuple[bool, str]:
    """无人值守场景（开机自启/计划任务）：用本机记住的口令自动拉取云端。"""
    conf = cb_cloud.load_conf()
    if not conf.get("slug"):
        return False, "未建立云端账号库"
    passphrase = cb_cloud.saved_passphrase()
    if not passphrase:
        return False, "本机未保存同步口令"
    return cloud_pull(store, passphrase)


# ---------- WorkBuddy 积分签到 ----------
def _save_checkin(store: AccountStore, rec: dict, res: dict) -> None:
    """把一次签到结果记回账号记录（供列表展示今日状态）。"""
    rec["checkin"] = {
        "date": time.strftime("%Y-%m-%d"),
        "status": res.get("status") or "",
        "label": res.get("label") or "",
        "credit": res.get("credit") or 0,
        "streak_days": res.get("streak_days") or 0,
        "at": res.get("at") or _now(),
    }
    store.update(rec)


def checkin_account(store: AccountStore, account_id: str) -> tuple[bool, str]:
    """给单个账号调用 WorkBuddy 每日签到。返回 (是否成功, 明细)。

    "今日已签到"视为成功（幂等，重复调用无副作用）；token 失效会先自动续期再试一次。
    """
    rec = store.get(account_id)
    if not rec:
        return False, "账号不存在"
    auth = record_auth(rec)
    if not auth:
        return False, "该账号没有可用的 access token"

    res = wb_api.checkin(auth)
    if res.get("status") == wb_api.REQUEST_ERROR and auth.get("refreshToken"):
        try:
            new_auth = cb_api.refresh_auth(auth)
        except Exception:
            pass
        else:
            try:
                _rebuild_keys_with_auth(rec, new_auth)
            except Exception:
                pass
            res = wb_api.checkin(new_auth)

    ok = bool(res.get("ok")) or res.get("status") == wb_api.ALREADY_CLAIMED
    _save_checkin(store, rec, res)
    text = wb_api.result_text(res)
    cb_log.log(f"签到 | {rec.get('label')} | {'OK' if ok else 'FAIL'} | {text}", "checkin")
    return ok, text


def refresh_all_sessions(store: AccountStore) -> list[tuple[str, bool, str]]:
    """对账号库中全部账号用 refresh token 续期登录态（不写 IDE，只更新账号库）。

    返回 [(账号名, 是否成功, 明细)]。refresh token 已过期的账号会失败，属正常情况。
    """
    out: list[tuple[str, bool, str]] = []
    for rec in store.list_accounts():
        label = rec.get("label") or rec.get("uid") or "?"
        try:
            res = refresh_session(store, rec["id"])
            out.append((label, res.ok, res.message))
        except Exception as e:  # noqa: BLE001
            out.append((label, False, str(e)))
    ok_n = sum(1 for _, ok, _ in out if ok)
    cb_log.log(f"批量续期 | 成功 {ok_n}/{len(out)}", "refresh")
    return out


def checkin_all(store: AccountStore) -> list[tuple[str, bool, str]]:
    """给账号库中全部账号签到，返回 [(账号名, 是否成功, 明细)]。"""
    out = []
    for rec in store.list_accounts():
        try:
            ok, text = checkin_account(store, rec["id"])
        except Exception as e:  # noqa: BLE001
            ok, text = False, str(e)
        out.append((rec.get("label") or rec.get("uid") or "?", ok, text))
    return out


def refresh_checkin_status(store: AccountStore, account_id: str) -> tuple[bool, str]:
    """只查询签到状态（不领取）。返回 (是否成功, 展示文本)。"""
    rec = store.get(account_id)
    if not rec:
        return False, "账号不存在"
    auth = record_auth(rec)
    if not auth:
        return False, "该账号没有可用的 access token"
    got = wb_api.fetch_status(auth)
    if not got.get("ok"):
        cb_log.log(f"签到状态 | {rec.get('label')} | FAIL | {got.get('error')}", "checkin")
        return False, got.get("error") or "查询失败"
    rec["checkin_status"] = {"data": got.get("data") or {}, "at": _now()}
    store.update(rec)
    text = wb_api.status_text(got.get("data") or {})
    cb_log.log(f"签到状态 | {rec.get('label')} | OK | {text}", "checkin")
    return True, text


def current_ide_account() -> str | None:
    """返回当前 IDE 生效账号的描述（供界面显示）。"""
    try:
        got = cs.read_current_account()
        if not got:
            return None
        _, plain = got
        import json
        try:
            obj = json.loads(plain)
        except Exception:
            return plain[:60]
        acc = obj.get("account") if isinstance(obj, dict) else None
        name = None
        if isinstance(acc, dict):
            name = acc.get("label") or acc.get("nickname") or acc.get("uid")
        name = (name or obj.get("label") or obj.get("nickname")
                or obj.get("preferred_username") or obj.get("uid"))
        if name:
            return name
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            o = obj[0]
            return (o.get("label") or o.get("nickname") or o.get("email") or o.get("uid"))
        return "（未知账号）"
    except Exception:
        return None
