# -*- coding: utf-8 -*-
"""WorkBuddy 目标定位与登录态同步。

事实（本机实测）：WorkBuddy 是 CodeBuddy 的伴侣壳，它并不自己存一份账号
（`workbuddy-target.json` 里 `capabilities.accounts: false`），而是拉起 target 中声明的
CodeBuddy 主程序并复用其登录态。target 文件位于：

    %APPDATA%\\WorkDaddy\\workbuddy-target.json
      binary    -> 被拉起的 CodeBuddy 主程序
      dataRoot  -> 该实例的用户数据目录（其下 Local State + state.vscdb 才是登录态）
      sessionDb -> WorkBuddy 自己的会话库（不含登录态）

因此"把登录态同步到 WorkBuddy"= 用同一套 DPAPI + AES-256-GCM 算法，把目标账号的会话
明文加密写入 dataRoot 下的 state.vscdb。算法/存储布局与 CodeBuddy 完全一致，故直接复用
`cb_secrets`。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cb_secrets as cs

TARGET_FILE = Path(os.environ.get("APPDATA", "")) / "WorkDaddy" / "workbuddy-target.json"


def load_target() -> dict | None:
    """读取 WorkBuddy target 配置；不存在返回 None。"""
    try:
        if not TARGET_FILE.exists():
            return None
        return json.loads(TARGET_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _candidates(t: dict) -> list[tuple[Path, str]]:
    """候选数据目录：target 声明的优先，其后是 WorkBuddy / CodeBuddy CN 实际使用的目录。

    WorkBuddy 未成功拉起过自己的 CodeBuddy 实例时（本机现状）dataRoot 并不存在，
    此时回退到 IDE 真实数据目录——WorkBuddy 复用的就是这份登录态。
    """
    out: list[tuple[Path, str]] = []
    root = t.get("dataRoot")
    if root:
        out.append((Path(root), "target.dataRoot"))
    roam = Path(os.environ.get("APPDATA", ""))
    out.append((roam / "WorkDaddy", "WorkBuddy 数据目录"))
    out.append((roam / "CodeBuddy CN", "CodeBuddy CN 数据目录"))
    out.append((roam / "CodeBuddy", "CodeBuddy 数据目录"))
    return out


def _resolve() -> tuple[Path | None, Path | None, Path | None, str, str]:
    """解析 (root, Local State, state.vscdb, 错误说明, 来源说明)。"""
    t = load_target()
    if not t:
        return None, None, None, f"未找到 WorkBuddy 配置：{TARGET_FILE}", ""
    tried: list[str] = []
    for path, origin in _candidates(t):
        if not path.exists():
            continue
        db = cs.find_state_db(path)
        if db:
            return path, path / "Local State", db, "", origin
        tried.append(str(path))
    return (None, None, None,
            "WorkBuddy 数据目录均未就绪（" + (", ".join(tried) or "无候选")
            + "）：请先在 WorkBuddy / CodeBuddy 中登录一次", "")


def status_line() -> str:
    """给界面用的一行状态说明。"""
    t = load_target()
    if not t:
        return "WorkBuddy：未检测到配置"
    root, _, db, err, origin = _resolve()
    if err:
        return f"WorkBuddy：{err}"
    proc = Path(t.get("binary") or "").name or "?"
    return f"WorkBuddy：{proc} · 数据目录 {root}（{origin}）· 登录库 {db.name}"


def current_account() -> str | None:
    """读取 WorkBuddy 目标里当前生效的账号名（读不到返回 None）。"""
    _, ls, db, err, _ = _resolve()
    if err or ls is None or db is None:
        return None
    try:
        aes = cs.aes_key_from(ls)
    except Exception:
        return None
    got = cs.read_current_account(db, aes)
    if not got:
        return None
    try:
        obj = json.loads(got[1])
        acc = obj.get("account") if isinstance(obj, dict) else None
        if isinstance(acc, dict):
            return acc.get("label") or acc.get("nickname") or acc.get("uid")
        if isinstance(obj, dict):
            return obj.get("label") or obj.get("nickname") or obj.get("uid")
    except Exception:
        pass
    return "（未知账号）"


def sync_session(plain: str, *, backup: bool = True) -> tuple[bool, str]:
    """把一条会话明文写入 WorkBuddy 目标的登录库。返回 (是否成功, 说明)。"""
    root, ls, db, err, origin = _resolve()
    if err or ls is None or db is None:
        return False, err or "WorkBuddy 目标不可用"
    try:
        aes = cs.aes_key_from(ls)
        keys = cs.resolve_target_keys(db)
        entries = {k: cs.encrypt_v10(plain, aes) for k in keys}
        bak = cs.backup_db(db) if backup else None
        n = cs.write_secrets(entries, db=db)
    except Exception as e:  # noqa: BLE001
        return False, f"同步失败：{e}"
    msg = f"已写入 {n} 条登录态到 {db.name}（{origin}）"
    if bak:
        msg += f"，原登录态已备份 {Path(bak).name}"
    return True, msg
