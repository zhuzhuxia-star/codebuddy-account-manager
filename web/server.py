# -*- coding: utf-8 -*-
"""CodeBuddy 账号管理器 · 网页版（作者：这是哪头猪？）。

复用桌面版核心模块（cb_api / wb_api / wb_growth / cb_app / account_store /
cb_cloud），只替换 Windows 专属层：
  - DPAPI        -> cb_secrets.py 替身（本地密钥 AES-256-GCM 落盘加密）
  - Windows 计划任务 -> 本进程内的调度线程（每日全量 + 每 N 小时成长计划）
  - tkinter GUI  -> 本文件提供的网页界面

运行环境变量：
  APPDATA        数据目录的父目录（账号库落在 $APPDATA/CodeBuddyAccountManager/）
  CBW_PORT       监听端口（默认 8080）
  TZ             时区（调度按本地时间，建议 Asia/Shanghai）
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# APPDATA 必须在导入 account_store / cb_log / cb_cloud 之前就位（它们在导入时
# 按 APPDATA 计算存储路径）。
os.environ.setdefault("APPDATA", os.environ.get("CBW_DATA", str(Path.home() / ".cbam-web")))

import cb_app  # noqa: E402
import cb_cloud  # noqa: E402
import cb_ledger  # noqa: E402
import cb_log  # noqa: E402

try:
    import u3store  # noqa: E402  对象存储（/oos 页）；缺失/损坏不影响主服务
except Exception:  # noqa: BLE001
    u3store = None  # type: ignore
from account_store import AccountStore, parse_pasted_text, quota_columns  # noqa: E402

try:
    time.tzset()  # 按 TZ 环境变量生效（Linux）
except Exception:
    pass

DATA_DIR = Path(os.environ["APPDATA"]) / "CodeBuddyAccountManager"
WEB_CONF = DATA_DIR / "web.json"
PORT = int(os.environ.get("CBW_PORT", "8080"))

DEFAULT_CONF = {
    "pw_salt": "",
    "pw_hash": "",
    "setup_token": "",
    "endpoint": "https://www.codebuddy.cn",
    "daily_time": "09:05",
    "growth_every_hours": 2,
    "cloud_pass_enc": "",
    "last_daily_date": "",
    "last_growth_ts": 0,
}

_conf_lock = threading.Lock()
RUN_LOCK = threading.Lock()
_sessions: dict[str, float] = {}
_login_fails: dict[str, list[float]] = {}
_last_run: dict = {"title": "", "at": "", "lines": []}
_job_state: dict = {"running": False, "name": "", "started": 0.0}


# ---------- 配置 ----------
def load_conf() -> dict:
    with _conf_lock:
        try:
            data = json.loads(WEB_CONF.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**DEFAULT_CONF, **data}
        except Exception:
            pass
        return dict(DEFAULT_CONF)


def save_conf(conf: dict) -> None:
    with _conf_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WEB_CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def apply_endpoint(conf: dict) -> None:
    ep = (conf.get("endpoint") or "").strip()
    if ep:
        os.environ["CB_API_ENDPOINT"] = ep
    else:
        os.environ.pop("CB_API_ENDPOINT", None)


# ---------- 口令 ----------
def _pw_hash(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(dk).decode()


def set_password(conf: dict, password: str) -> None:
    salt = base64.b64encode(os.urandom(16)).decode()
    conf["pw_salt"] = salt
    conf["pw_hash"] = _pw_hash(password, salt)


def check_password(conf: dict, password: str) -> bool:
    if not conf.get("pw_hash"):
        return False
    return secrets.compare_digest(_pw_hash(password, conf["pw_salt"]),
                                  conf["pw_hash"])


def saved_cloud_passphrase(conf: dict) -> str:
    enc = conf.get("cloud_pass_enc") or ""
    if not enc:
        return ""
    try:
        import cb_secrets as cs
        return cs.dpapi_unprotect(base64.b64decode(enc)).decode("utf-8")
    except Exception:
        return ""


def save_cloud_passphrase(conf: dict, passphrase: str) -> None:
    import cb_secrets as cs
    conf["cloud_pass_enc"] = base64.b64encode(
        cs.dpapi_protect(passphrase.encode("utf-8"))).decode()


# ---------- 会话 ----------
def new_session() -> str:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + 30 * 86400
    return tok


def session_ok(tok: str | None) -> bool:
    if not tok:
        return False
    exp = _sessions.get(tok)
    if not exp or exp < time.time():
        _sessions.pop(tok, None)
        return False
    return True


# ---------- 任务执行（续期/签到/成长计划/额度，全部串行） ----------
def _fmt_results(title: str, results) -> dict:
    lines = [f"[{'OK  ' if ok else 'FAIL'}] {label} | {text}"
             for label, ok, text in results]
    ok_n = sum(1 for _, ok, _ in results if ok)
    lines.append(f"完成 {ok_n}/{len(results)}")
    return {"title": title, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lines": lines}


def _quota_results(store: AccountStore):
    res = []
    for rec in store.list_accounts():
        ok, text = cb_app.refresh_quota(store, rec["id"])
        res.append((rec.get("label") or rec.get("uid") or "?", ok, text))
    return res


def job_quota_all() -> dict:
    return _fmt_results("额度刷新", _quota_results(AccountStore()))


def job_full() -> dict:
    """与桌面版 checkin-all 一致并加强：续期 → 刷新额度 → 签到 →
    活跃上报 → 积分补偿/礼包 → 成长计划（含连登兑换/抽奖）。"""
    store = AccountStore()
    out = []
    out += _fmt_results("续期登录态", cb_app.refresh_all_sessions(store))["lines"]
    out += _fmt_results("额度刷新", _quota_results(store))["lines"]
    out += _fmt_results("每日签到", cb_app.checkin_all(store))["lines"]
    out += _fmt_results("活跃上报", cb_app.activity_all(store))["lines"]
    out += _fmt_results("积分补偿/礼包", cb_app.compensation_all(store))["lines"]
    out += _fmt_results("成长计划", cb_app.growth_all(store))["lines"]
    return {"title": "续期 + 额度 + 签到 + 活跃 + 补偿 + 成长计划",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "lines": out}


def job_growth() -> dict:
    # 每 2 小时的定时轮：跳过连登兑换/抽奖（放每日全量，避免超频）
    return _fmt_results("成长计划", cb_app.growth_all(AccountStore(), daily=False))


def job_status() -> dict:
    store = AccountStore()
    res = []
    for rec in store.list_accounts():
        ok, text = cb_app.refresh_checkin_status(store, rec["id"])
        res.append((rec.get("label") or rec.get("uid") or "?", ok, text))
    return _fmt_results("签到状态查询", res)


def job_single(kind: str, account_id: str) -> dict:
    store = AccountStore()
    rec = store.get(account_id)
    label = (rec or {}).get("label") or account_id
    if kind == "checkin":
        ok, text = cb_app.checkin_account(store, account_id)
        return _fmt_results("单账号签到", [(label, ok, text)])
    if kind == "refresh":
        r = cb_app.refresh_session(store, account_id)
        return _fmt_results("单账号续期", [(label, r.ok, r.message)])
    if kind == "quota":
        ok, text = cb_app.refresh_quota(store, account_id)
        return _fmt_results("单账号额度", [(label, ok, text)])
    if kind == "liveness":
        ok, text = cb_app.liveness_account(store, account_id)
        return _fmt_results("单账号测活", [(label, ok, text)])
    raise ValueError(kind)


def run_job(name: str, fn, *, blocking: bool = False) -> dict | None:
    """串行执行任务并记录结果。已有任务在跑时：手动触发直接返回提示，
    调度触发（blocking=False 且拿不到锁）静默跳过。"""
    global _last_run
    if not RUN_LOCK.acquire(blocking=blocking):
        if blocking:
            return None
        return {"title": name, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lines": ["已有任务正在执行，本次跳过"]}
    _job_state.update(running=True, name=name, started=time.time())
    try:
        cb_log.log(f"网页版任务开始 | {name}", "web")
        result = fn()
        _last_run = result
        cb_log.log(f"网页版任务完成 | {name} | "
                   f"{result['lines'][-1] if result['lines'] else ''}", "web")
        return result
    except Exception as e:  # noqa: BLE001
        result = {"title": name, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "lines": [f"[FAIL] 执行出错：{e}"]}
        _last_run = result
        cb_log.log(f"网页版任务出错 | {name} | {e}", "web")
        return result
    finally:
        _job_state.update(running=False, name="", started=0.0)
        RUN_LOCK.release()


def run_job_async(name: str, fn) -> None:
    threading.Thread(target=run_job, args=(name, fn), daemon=True).start()


# ---------- 在线登录添加账号（官方登录页完成手机号+短信验证） ----------
_pending_logins: dict[str, dict] = {}


def _login_worker(key: str) -> None:
    """走桌面版同款官方登录流程：本服务向网关申请登录地址，用户在腾讯官方
    页面上用手机号+短信验证码完成登录，服务轮询取回 token 并入库。
    短信验证码只出现在官方页面，不经过本服务。"""
    info = _pending_logins[key]
    store = AccountStore()

    def on_url(url):
        info["url"] = url

    def on_status(text):
        info["status"] = text

    try:
        rec = cb_app.login_account(on_auth_url=on_url, on_status=on_status)
        with RUN_LOCK:
            store.add(rec)
            try:
                cb_app.refresh_quota(store, rec["id"])
            except Exception:
                pass
        info["done"] = True
        info["status"] = f"登录成功，已添加账号：{rec.get('label')}"
        cb_log.log(f"在线登录 | OK | {rec.get('label')}", "web")
    except Exception as e:  # noqa: BLE001
        info["done"] = True
        info["error"] = str(e)
        info["status"] = f"登录失败：{e}"
        cb_log.log(f"在线登录 | FAIL | {e}", "web")


def start_online_login() -> str:
    key = secrets.token_urlsafe(12)
    _pending_logins[key] = {"created": time.time(), "status": "正在获取登录地址…",
                            "url": "", "done": False, "error": ""}
    threading.Thread(target=_login_worker, args=(key,), daemon=True).start()
    return key


# ---------- 调度线程（替代 Windows 计划任务） ----------
def scheduler_loop() -> None:
    while True:
        try:
            conf = load_conf()
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            hm = time.strftime("%H:%M", now)
            if conf.get("pw_hash"):  # 未完成初始化时不跑任务
                if hm >= (conf.get("daily_time") or "09:05") \
                        and conf.get("last_daily_date") != today \
                        and AccountStore().list_accounts():
                    conf["last_daily_date"] = today
                    save_conf(conf)
                    run_job("每日定时（续期+签到+成长计划）", job_full)
                    conf = load_conf()
                    conf["last_growth_ts"] = int(time.time())
                    save_conf(conf)
                every = int(conf.get("growth_every_hours") or 0)
                if every > 0 and time.time() - float(conf.get("last_growth_ts") or 0) \
                        >= every * 3600:
                    conf["last_growth_ts"] = int(time.time())
                    save_conf(conf)
                    run_job("定时成长计划", job_growth)
        except Exception as e:  # noqa: BLE001
            cb_log.log(f"调度线程异常：{e}", "web")
        time.sleep(30)


# ---------- 页面 ----------
CSS = """
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
margin:0;background:#f4f5f7;color:#1c1e21}
.wrap{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}
h2{font-size:16px;margin:20px 0 8px}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border-bottom:1px solid #eee;padding:7px 6px;text-align:left;vertical-align:top}
button,.btn{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:7px;
padding:7px 13px;font-size:14px;cursor:pointer;text-decoration:none;margin:2px 2px 2px 0}
button.gray,.btn.gray{background:#6b7280}button.red{background:#dc2626}
button:disabled{background:#9ca3af}
input,textarea,select{font-size:14px;padding:7px;border:1px solid #c9ccd1;border-radius:7px;
width:100%;box-sizing:border-box;margin:4px 0 10px}
textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.msg{background:#ecfdf5;border:1px solid #10b981;color:#065f46;border-radius:8px;
padding:9px 12px;margin:10px 0;font-size:14px}
.err{background:#fef2f2;border-color:#ef4444;color:#991b1b}
pre{background:#111827;color:#d1d5db;border-radius:8px;padding:10px;overflow:auto;
font-size:12px;max-height:420px;white-space:pre-wrap;word-break:break-all}
.small{color:#6b7280;font-size:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
form.inline{display:inline}
.acts{display:grid;grid-template-columns:repeat(3,74px);gap:6px}
.acts form{margin:0}
.acts button,.acts .btn{display:block;width:100%;box-sizing:border-box;margin:0;
padding:6px 0;text-align:center;font-size:13px;border-radius:6px}
.acts button.red{background:#fff;color:#dc2626;border:1px solid #f0b8b2}
td form{margin:0}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #bfdbfe;
border-top-color:#2563eb;border-radius:50%;animation:sp .8s linear infinite;
vertical-align:-2px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px;margin:10px 0}
.acard{background:#fff;border:1px solid #e2e4e9;border-radius:14px;padding:14px 16px;
box-shadow:0 1px 3px rgba(16,24,40,.06)}
.ahead{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.aname{font-size:16px;font-weight:600;word-break:break-all}
.apill{flex:none;background:#eef2ff;color:#3730a3;border-radius:999px;
padding:2px 10px;font-size:12px;white-space:nowrap}
.abig{display:flex;align-items:baseline;gap:9px;margin:10px 0 0;flex-wrap:wrap}
.abig .num{font-size:26px;font-weight:700}
.ameta{display:flex;gap:6px 12px;flex-wrap:wrap;color:#6b7280;font-size:12px;margin:7px 0 0}
.pkhead{display:flex;justify-content:space-between;align-items:center;
border-top:1px solid #f0f1f4;margin-top:11px;padding-top:10px;
font-size:13px;color:#374151;font-weight:600}
.pkrow{display:flex;align-items:baseline;gap:8px;font-size:13px;margin-top:8px}
.pkleft{font-weight:600;white-space:nowrap}
.pkname{color:#6b7280;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pkexp{color:#6b7280;white-space:nowrap;font-size:12px}
.pkexp.soon{color:#ea580c;font-weight:600}
.pkbar{height:3px;background:#f3f4f6;border-radius:2px;margin-top:3px;overflow:hidden}
.pkbar i{display:block;height:100%;background:#f97316;border-radius:2px}
.aacts{display:flex;gap:6px;flex-wrap:wrap;border-top:1px solid #f0f1f4;
margin-top:12px;padding-top:10px}
.aacts form{margin:0}
.aacts button,.aacts .btn{margin:0;padding:6px 12px;font-size:13px;border-radius:6px}
.aacts button.red{background:#fff;color:#dc2626;border:1px solid #f0b8b2}
#jobbar{display:none;background:#eff6ff;border:1px solid #3b82f6;color:#1e3a8a;
border-radius:8px;padding:9px 12px;margin:10px 0;font-size:14px}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def page(title: str, body: str, msg: str = "", is_err: bool = False) -> bytes:
    m = f'<div class="msg{" err" if is_err else ""}">{esc(msg)}</div>' if msg else ""
    doc = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{esc(title)}</title><style>{CSS}</style></head>"
           f"<body><div class='wrap'>{m}{body}</div></body></html>")
    return doc.encode("utf-8")


def login_page(msg: str = "", is_err: bool = False) -> bytes:
    body = """<h1>CodeBuddy 账号管理器 · 网页版</h1>
<div class="card"><form method="post" action="/login">
<label>管理密码</label><input type="password" name="password" autofocus required>
<button type="submit">登录</button></form></div>"""
    return page("登录", body, msg, is_err)


def setup_page(token: str, msg: str = "", is_err: bool = False) -> bytes:
    body = f"""<h1>首次设置</h1>
<div class="card"><p>设置一个管理密码（打开本页面需要它）。初始化令牌已预填，
设好密码后令牌立即作废。</p>
<form method="post" action="/setup">
<label>初始化令牌</label><input name="token" value="{esc(token)}" required>
<label>管理密码（至少 6 位）</label><input type="password" name="password" required>
<button type="submit">设置密码并进入</button></form></div>"""
    return page("首次设置", body, msg, is_err)


DASH_JS = """
<script>
const bar = document.getElementById('jobbar');
let wasRunning = false;
function showBar(html) { bar.style.display = 'block'; bar.innerHTML = html; }
function setBusy(busy) {
  document.querySelectorAll('button').forEach(b => { b.disabled = busy; });
}
async function poll() {
  try {
    const r = await fetch('/jobstatus', { headers: { 'Accept': 'application/json' } });
    const j = await r.json();
    if (j.running) {
      wasRunning = true;
      setBusy(true);
      showBar('<span class="spin"></span>正在执行：' + j.name +
              '（已 ' + j.elapsed + ' 秒），完成后本页会自动刷新显示结果…');
    } else if (wasRunning) {
      showBar('✅ 执行完成，正在刷新页面…');
      setTimeout(() => location.reload(), 700);
      return;
    }
  } catch (e) {}
  setTimeout(poll, 1500);
}
poll();
document.querySelectorAll('form').forEach(f => {
  f.addEventListener('submit', () => {
    const btn = f.querySelector('button');
    setBusy(true);
    if (btn) btn.innerHTML = '<span class="spin"></span>执行中…';
    showBar('<span class="spin"></span>已提交，正在执行…');
  });
});
</script>"""


def _full_phone(rec: dict) -> str:
    """从存量登录态里还原完整手机号：JWT 的 preferred_username 优先，
    其次 account.phoneNumber（未打码时）。取不到返回空串。"""
    for v in (rec.get("keys") or {}).values():
        try:
            sess = json.loads(v)
        except Exception:
            continue
        auth = sess.get("auth") or {}
        tok = str(auth.get("accessToken") or "")
        parts = tok.split(".")
        if len(parts) == 3:
            try:
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                pu = str(claims.get("preferred_username") or "")
                if len(pu) == 11 and pu.isdigit():
                    return pu
            except Exception:
                pass
        pn = str((sess.get("account") or {}).get("phoneNumber") or "")
        if len(pn) == 11 and pn.isdigit():
            return pn
    return ""


def _fmt_credits(v) -> str:
    """积分数格式化：千分位；整数不带小数，非整数最多两位小数（与参考样式一致）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


def _pkg_rows_html(q: dict) -> tuple[str, int, int]:
    """渲染「近期到期」积分包行：只列 left>0、按到期时间从近到远、最多 4 条。

    返回 (html, 包总数, 未列出的剩余包数)。expire_ms 为 0/缺失的包排最后、
    到期日显示「—」；3 天内到期（含缓存已过期）标橙。每行下带一条
    left/total 比例的细进度条，与参考卡片样式一致。
    """
    pkgs = [p for p in (q.get("packages") or []) if isinstance(p, dict)]
    live = [p for p in pkgs if (p.get("left") or 0) > 0]
    live.sort(key=lambda p: (not (p.get("expire_ms") or 0), p.get("expire_ms") or 0))
    now_ms = int(time.time() * 1000)
    rows = []
    for p in live[:4]:
        ems = p.get("expire_ms") or 0
        if ems:
            d = datetime.fromtimestamp(ems / 1000)
            exp = f"{d.month}/{d.day} 到期"
            soon = ems - now_ms <= 3 * 86400 * 1000
        else:
            exp, soon = "—", False
        total = p.get("total") or 0
        pct = min(100.0, max(0.0, (p.get("left") or 0) / total * 100)) if total else 100.0
        rows.append(
            f"<div class='pkrow'><span class='pkleft'>{esc(_fmt_credits(p.get('left')))} 积分</span>"
            f"<span class='pkname'>{esc(p.get('name') or '')}</span>"
            f"<span class='pkexp{' soon' if soon else ''}'>{esc(exp)}</span></div>"
            f"<div class='pkbar'><i style='width:{pct:.0f}%'></i></div>")
    if not rows:
        rows.append("<div class='small' style='margin-top:8px'>暂无积分包明细，点下方「额度」刷新</div>")
    return "".join(rows), len(pkgs), max(0, len(live) - 4)


def dashboard(msg: str = "", is_err: bool = False) -> bytes:
    conf = load_conf()
    store = AccountStore()
    accounts = store.list_accounts()
    today = time.strftime("%Y-%m-%d")

    cards = []
    for rec in accounts:
        plan, _quota_text = quota_columns(rec)
        q = rec.get("quota") or {}
        if not isinstance(q, dict):
            q = {}
        ck = rec.get("checkin") or {}
        if ck.get("date") == today:
            ck_text = f"{ck.get('label') or ck.get('status') or '-'}"
            if ck.get("credit"):
                ck_text += f" +{ck['credit']}"
            if ck.get("streak_days"):
                ck_text += f" · 连签 {ck['streak_days']} 天"
        else:
            ck_text = "今日未签"
        gr = rec.get("growth") or {}
        gr_text = gr.get("text") if gr.get("date") == today else "-"
        lv = rec.get("liveness") or {}
        if lv:
            lv_at = str(lv.get("at") or "")
            lv_text = f"{lv_at[5:16]} {lv.get('text') or ''}"
            if lv.get("status") == "auth_dead":
                lv_html = (f"<span style='color:#b91c1c;font-weight:600'>"
                           f"测活：{esc(lv_text)}</span>")
            elif lv.get("status") == "error":
                lv_html = f"<span style='color:#b45309'>测活：{esc(lv_text)}</span>"
            else:
                lv_html = f"<span>测活：{esc(lv_text)}</span>"
        else:
            lv_html = "<span>测活：未测</span>"
        dead_badge = ("<span class='apill' style='background:#fef2f2;color:#b91c1c'>"
                      "需重新登录</span>"
                      if lv.get("status") == "auth_dead" else "")
        rid = esc(rec.get("id") or "")
        phone = _full_phone(rec)
        label = str(rec.get('label') or rec.get('uid') or '?')
        disp = phone if ('****' in label and phone) else label
        sub = esc(rec.get('uid') or '')
        if phone and phone != disp:
            sub = (sub + ' · ' if sub else '') + '手机 ' + esc(phone)
        # 大字剩余总积分（兼容旧缓存 total_remain）
        left = q.get("usage_left")
        if left is None:
            left = q.get("total_remain")
        if q.get("error") and left is None:
            big = "<span class='num' style='font-size:18px'>查询失败</span>"
        elif left is None:
            big = "<span class='num'>-</span>"
        else:
            big = f"<span class='num'>⚡ {_fmt_credits(left)}</span>"
        pkg_html, pkg_cnt, pkg_more = _pkg_rows_html(q)
        cnt_html = f"<span class='small'>{pkg_cnt} 个积分包</span>" if pkg_cnt else ""
        upd = str(q.get("updated_at") or "")
        upd_html = f"<span class='small'>{esc(upd[11:16])} 更新</span>" if len(upd) >= 16 else ""
        more_html = (f"<span class='small'>还有 {pkg_more} 个未列出</span>"
                     if pkg_more else "")
        cards.append(
            f"<div class='acard'>"
            f"<div class='ahead'><div><div class='aname'>{esc(disp)}</div>"
            f"<div class='small'>{sub}</div></div>"
            f"<span>{dead_badge}<span class='apill'>{esc(plan)}</span></span></div>"
            f"<div class='abig'>{big}{cnt_html}{upd_html}</div>"
            f"<div class='ameta'><span>签到：{esc(ck_text)}</span>"
            f"<span>成长计划：{esc(gr_text or '-')}</span>"
            f"{lv_html}"
            f"<span>上次续期：{esc(rec.get('refreshed_at') or '-')}</span></div>"
            f"<div class='pkhead'><span>近期到期</span>{more_html}</div>"
            f"{pkg_html}"
            f"<div class='aacts'>"
            f"<form method='post' action='/run/checkin'>"
            f"<input type='hidden' name='id' value='{rid}'><button>签到</button></form>"
            f"<form method='post' action='/run/refresh'>"
            f"<input type='hidden' name='id' value='{rid}'><button class='gray'>续期</button></form>"
            f"<form method='post' action='/run/quota'>"
            f"<input type='hidden' name='id' value='{rid}'><button class='gray'>额度</button></form>"
            f"<form method='post' action='/run/liveness'>"
            f"<input type='hidden' name='id' value='{rid}'><button class='gray'>测活</button></form>"
            f"<a class='btn gray' href='/script?id={rid}'>登录脚本</a>"
            f"<form method='post' action='/account/delete' "
            f"onsubmit='return confirm(\"确定删除这个账号？\")'>"
            f"<input type='hidden' name='id' value='{rid}'><button class='red'>删除</button></form>"
            f"</div></div>")

    last = ""
    if _last_run.get("lines"):
        last = (f"<div class='card'><h2>最近一次执行：{esc(_last_run.get('title'))}"
                f" <span class='small'>{esc(_last_run.get('at'))}</span></h2><pre>"
                + esc("\n".join(_last_run["lines"])) + "</pre></div>")

    body = f"""<h1>CodeBuddy 账号管理器 · 网页版</h1>
<div id="jobbar"></div>
<div class="row">
<form class="inline" method="post" action="/run/full"><button>立即执行：续期+额度+签到+活跃+补偿+成长计划</button></form>
<form class="inline" method="post" action="/run/quota-all"><button class="gray">刷新全部额度</button></form>
<form class="inline" method="post" action="/run/status"><button class="gray">只查签到状态</button></form>
<form class="inline" method="post" action="/run/growth"><button class="gray">只跑成长计划</button></form>
<a class="btn gray" href="/import">导入账号</a>
<a class="btn gray" href="/ledger">积分获取</a>
<a class="btn gray" href="/settings">设置</a>
<a class="btn gray" href="/logs">日志</a>
<a class="btn gray" href="/oos">对象存储</a>
<form class="inline" method="post" action="/logout"><button class="gray">退出</button></form>
</div>
<div class="card small">共 {len(accounts)} 个账号 · 网关 {esc(conf.get('endpoint') or '默认')}
· 每日 {esc(conf.get('daily_time') or '09:05')} 自动全量 · 成长计划每 {esc(conf.get('growth_every_hours'))} 小时
· {esc(cb_app.cloud_status())}</div>
{('<div class="cards">' + ''.join(cards) + '</div>') if cards else '<div class="card">还没有账号，去「导入账号」粘贴登录态或用云端同步拉取。</div>'}
{last}""" + DASH_JS
    return page("账号列表", body, msg, is_err)


def import_page(msg: str = "", is_err: bool = False) -> bytes:
    body = """<h1>导入账号</h1>
<div class="card"><h2>方式一：在线登录（手机号）</h2>
<p class="small">点下面按钮，服务器会向 CodeBuddy 网关申请一个官方登录链接；
你在腾讯官方页面上用<b>手机号 + 短信验证码</b>完成登录，服务器会自动取回
登录态并入库（顺带查一次额度）。验证码只在官方页面输入，不经过本服务。
链接 5 分钟内有效。</p>
<form method="post" action="/login-online/start">
<button type="submit">获取官方登录链接</button></form></div>
<div class="card"><h2>方式二：粘贴登录态 JSON</h2>
<p class="small">和桌面版一样：粘贴 CodeBuddy 账号登录态 JSON（auth_raw/profile_raw 导出视图、
account+auth 明文、或松散的 access_token/refresh_token 对象），可一次多条。</p>
<form method="post" action="/import/paste">
<textarea name="text" rows="9" placeholder="把登录态 JSON 粘贴到这里"></textarea>
<button type="submit">导入</button></form></div>
<div class="card"><h2>方式三：从云端保险库同步</h2>
<p class="small">先在桌面版点一次「云端同步…→上传」，再在这里填同一个同步码拉取全部账号。
同步码只用于本次拉取；想让服务器记住它（供定时推送/拉取），去设置页保存。</p>
<form method="post" action="/import/cloud">
<label>同步码</label><input name="passphrase" autocomplete="off">
<label>文章 slug / URL（可空，默认 CBAC-VAULT-v1）</label><input name="ref">
<button type="submit">从云端拉取并合并</button></form></div>
<p><a class="btn gray" href="/">返回账号列表</a></p>"""
    return page("导入账号", body, msg, is_err)


def online_login_page(key: str) -> bytes:
    info = _pending_logins.get(key)
    if not info:
        return page("在线登录", "<h1>在线登录</h1><div class='card'>这次登录已过期，"
                                "请回导入页重新获取。</div>"
                                "<p><a class='btn gray' href='/import'>去导入页</a></p>")
    refresh = "" if info.get("done") else "<meta http-equiv='refresh' content='5'>"
    url_btn = ""
    if info.get("url") and not info.get("done"):
        url_btn = (f"<p><a class='btn' href='{esc(info['url'])}' target='_blank' "
                   f"rel='noopener'>打开官方登录页</a></p>"
                   f"<p class='small'>在打开的腾讯官方页面用手机号 + 短信验证码完成登录，"
                   f"完成后回到本页，本页每 5 秒自动刷新，成功后账号会自动入库。</p>")
    done_btn = ("<p><a class='btn' href='/'>回账号列表查看</a></p>"
                if info.get("done") and not info.get("error") else "")
    body = (f"<h1>在线登录添加账号</h1><div class='card'>"
            f"<p>{esc(info.get('status') or '')}</p>{url_btn}{done_btn}</div>"
            f"<p><a class='btn gray' href='/import'>返回导入页</a></p>")
    doc = page("在线登录", body)
    if refresh:  # 在 <head> 注入自动刷新
        doc = doc.replace(b"<title>", refresh.encode() + b"<title>", 1)
    return doc


def settings_page(msg: str = "", is_err: bool = False) -> bytes:
    conf = load_conf()
    has_pass = bool(saved_cloud_passphrase(conf))
    body = f"""<h1>设置</h1>
<div class="card"><form method="post" action="/settings">
<label>网关 endpoint（国内版 https://www.codebuddy.cn；国际版 https://copilot.tencent.com）</label>
<input name="endpoint" value="{esc(conf.get('endpoint') or '')}">
<label>每日全量执行时间（续期+签到+成长计划）</label>
<input name="daily_time" value="{esc(conf.get('daily_time') or '09:05')}">
<label>成长计划间隔小时（0 = 只跟每日全量跑）</label>
<input name="growth_every_hours" type="number" min="0" max="24" value="{esc(conf.get('growth_every_hours'))}">
<label>云端同步码（{'已保存，留空 = 不改' if has_pass else '未保存；填入后服务器可定时推送/拉取'})</label>
<input name="cloud_passphrase" autocomplete="off">
<label>新管理密码（留空 = 不改）</label>
<input type="password" name="new_password" autocomplete="new-password">
<button type="submit">保存设置</button></form></div>
<div class="card"><h2>把服务器最新登录态推回云端</h2>
<p class="small">续期会换新 token，推回云端后桌面版再「从云端同步」就能拿到最新登录态。</p>
<form method="post" action="/cloud/push"><button type="submit">立即推送到云端保险库</button></form></div>
<p><a class="btn gray" href="/">返回账号列表</a></p>"""
    return page("设置", body, msg, is_err)


def logs_page() -> bytes:
    lines = cb_log.tail(300)
    body = ("<h1>运行日志</h1><pre>" + esc("\n".join(lines)) +
            "</pre><p><a class='btn gray' href='/'>返回账号列表</a></p>")
    return page("运行日志", body)


def ledger_page(account: str = "", source: str = "") -> bytes:
    """积分获取记账页：逐笔流水（时间倒序），可按账号/来源筛选，
    顶部给今日/累计汇总与按账号小计。历史无法回填，自上线起记录。"""
    s = cb_ledger.summary()
    entries = cb_ledger.list_entries(uid=account, source=source, limit=300)

    store = AccountStore()
    acc_opts = ["<option value=''>全部账号</option>"]
    seen = set()
    for rec in store.list_accounts():
        uid = rec.get("uid") or ""
        if not uid or uid in seen:
            continue
        seen.add(uid)
        sel = " selected" if uid == account else ""
        acc_opts.append(f"<option value='{esc(uid)}'{sel}>"
                        f"{esc(rec.get('label') or uid)}</option>")
    for a in cb_ledger.accounts_in_entries():
        if a["uid"] and a["uid"] not in seen:
            seen.add(a["uid"])
            sel = " selected" if a["uid"] == account else ""
            acc_opts.append(f"<option value='{esc(a['uid'])}'{sel}>"
                            f"{esc(a['label'] or a['uid'])}</option>")
    src_opts = ["<option value=''>全部来源</option>"]
    for k, v in cb_ledger.SOURCES.items():
        sel = " selected" if k == source else ""
        src_opts.append(f"<option value='{k}'{sel}>{v}</option>")

    def _sum_txt(credit, unknown, count) -> str:
        t = f"+{_fmt_credits(credit)} 积分 · {count} 笔"
        if unknown:
            t += f"（另 {unknown} 笔数量未知）"
        return t

    per_acc = []
    for uid, pa in sorted(s["per_account"].items(),
                          key=lambda kv: -kv[1]["credit"]):
        name = pa["label"] or uid or "?"
        if pa["phone_tail"]:
            name += f"（尾号 {pa['phone_tail']}）"
        per_acc.append(f"<tr><td>{esc(name)}</td>"
                       f"<td>{_sum_txt(pa['credit'], pa['unknown'], pa['count'])}</td></tr>")

    rows = []
    for e in entries:
        credit = e.get("credit")
        c_txt = (f"+{_fmt_credits(credit)}" if isinstance(credit, (int, float))
                 else "数量未知")
        name = e.get("label") or e.get("uid") or "?"
        if e.get("phone_tail"):
            name += f"（尾号 {e['phone_tail']}）"
        note = e.get("note") or ""
        detail = e.get("detail") or ""
        rows.append(
            f"<tr><td style='white-space:nowrap'>{esc(e.get('ts') or '')}</td>"
            f"<td>{esc(name)}</td>"
            f"<td>{esc(cb_ledger.SOURCES.get(e.get('source') or '', e.get('source') or ''))}</td>"
            f"<td>{esc(detail)}</td><td>{esc(c_txt)}</td>"
            f"<td class='small'>{esc(note)}</td></tr>")

    body = f"""<h1>积分获取</h1>
<p><a class="btn gray" href="/">返回账号列表</a></p>
<div class="card"><b>今日</b> {_sum_txt(s['today_credit'], s['today_unknown'], s['today_count'])}
 · <b>累计</b> {_sum_txt(s['total_credit'], s['total_unknown'], s['total_count'])}
<span class="small">（自记账功能上线起记录，历史进账无法回填；数量未知的笔数不计入金额）</span></div>
{('<div class="card"><h2>按账号小计（累计）</h2><table>' + ''.join(per_acc) + '</table></div>') if per_acc else ''}
<div class="card"><form method="get" action="/ledger" class="row">
<label>账号 <select name="account" style="width:auto">{''.join(acc_opts)}</select></label>
<label>来源 <select name="source" style="width:auto">{''.join(src_opts)}</select></label>
<button type="submit">筛选</button></form>
<table><tr><th>时间</th><th>账号</th><th>来源</th><th>明细</th><th>积分</th><th>备注</th></tr>
{''.join(rows) if rows else '<tr><td colspan="6">暂无记录（签到、成长任务等产生进账后会出现在这里）</td></tr>'}
</table></div>"""
    return page("积分获取", body)


# ---------- 一键登录脚本（写入任意 Windows 电脑的 WorkBuddy / CodeBuddy） ----------
PS_TEMPLATE = (Path(__file__).parent / "inject.ps1").read_text(encoding="utf-8")
_scripts: dict[str, tuple[float, str, str]] = {}


def _pick_session_plain(rec: dict) -> str | None:
    """从账号记录里挑一份 IDE 会话明文（顶层含 account+auth+accessToken）。"""
    from account_store import build_ide_session
    fallback = None
    for plain in (rec.get("keys") or {}).values():
        try:
            obj = json.loads(plain)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        auth = obj.get("auth") if isinstance(obj.get("auth"), dict) else {}
        if auth.get("accessToken") and obj.get("accessToken"):
            return plain
        if auth.get("accessToken") or obj.get("accessToken"):
            fallback = fallback or obj
    if fallback is not None:
        return build_ide_session(fallback)
    return None


def build_script(aid: str):
    """生成一键登录 .cmd。返回 (cmd_text, filename, warn)；账号不存在返回 None。

    生成前先给该账号续期一次，内嵌最新登录态；脚本在目标电脑上还会再续期一次，
    然后把登录态写进 WorkBuddy 的 .info 与 CodeBuddy 的 state.vscdb。
    """
    store = AccountStore()
    rec = store.get(aid)
    if not rec:
        return None
    warn = ""
    with RUN_LOCK:
        try:
            cb_app.refresh_session(store, aid)
            rec = store.get(aid) or rec
        except Exception as e:  # noqa: BLE001
            warn = f"生成前续期失败（{e}），已内嵌现有登录态；脚本运行时会再自动续期一次。"
    plain = _pick_session_plain(rec)
    if not plain:
        return "", "", "该账号没有可用的登录态，无法生成脚本"
    conf = load_conf()
    label = rec.get("label") or rec.get("uid") or "account"
    ps = PS_TEMPLATE.replace(
        "__SESSION_B64__", base64.b64encode(plain.encode("utf-8")).decode())
    ps = ps.replace("__ENDPOINT__",
                    (conf.get("endpoint") or "https://www.codebuddy.cn").rstrip("/"))
    ps = ps.replace("__LABEL__", str(label).replace("'", "''"))
    blob = base64.b64encode(ps.encode("utf-8")).decode()
    chunks = [blob[i:i + 76] for i in range(0, len(blob), 76)]
    lines = ["@echo off", "setlocal EnableExtensions", "chcp 65001 >nul 2>&1",
             "title CodeBuddy / WorkBuddy one-click login",
             'set "WORK=%TEMP%\\cbam-inject-%RANDOM%"',
             'mkdir "%WORK%" >nul 2>&1',
             'set "B64=%WORK%\\payload.b64"',
             f'> "%B64%" echo {chunks[0]}']
    lines += [f'>> "%B64%" echo {c}' for c in chunks[1:]]
    lines.append(
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        '"$b=[IO.File]::ReadAllText(\'%B64%\');$p=\'%WORK%\\inject.ps1\';'
        '[IO.File]::WriteAllBytes($p,[byte[]](0xEF,0xBB,0xBF)'
        '+[Convert]::FromBase64String($b)); & $p"')
    lines += ['rd /s /q "%WORK%" >nul 2>&1', "echo.", "pause"]
    safe = "".join(c if (c.isascii() and (c.isalnum() or c in "._-")) else "_"
                   for c in str(label))[:24].strip("_") or "account"
    return "\r\n".join(lines) + "\r\n", f"cbam-login-{safe}.cmd", warn


def script_page(aid: str) -> bytes:
    got = build_script(aid)
    if got is None:
        return page("登录脚本", "<h1>一键登录脚本</h1><div class='card'>账号不存在。</div>"
                                "<p><a class='btn gray' href='/'>返回账号列表</a></p>")
    text, fname, warn = got
    if not text:
        return page("登录脚本", f"<h1>一键登录脚本</h1><div class='card'>{esc(warn)}</div>"
                                "<p><a class='btn gray' href='/'>返回账号列表</a></p>")
    _scripts[aid] = (time.time(), text, fname)
    warn_html = f"<p class='small' style='color:#b91c1c'>{esc(warn)}</p>" if warn else ""
    body = f"""<h1>一键登录脚本</h1>
<div class="card"><p>这个脚本把该账号的登录态写入<b>任意一台 Windows 电脑</b>的
WorkBuddy 桌面版和 CodeBuddy IDE，打开客户端就是这个账号，不用再走手机号登录。</p>
<p class="small">用法：点「一键复制」，在目标电脑上新建一个文本文件、粘贴、保存为
<b>{esc(fname)}</b>（扩展名必须是 .cmd），双击运行；也可以直接点「下载 .cmd 文件」拷过去运行。
运行过程：先续期换新 token → 关闭正在运行的客户端 → 写入登录态（原登录态会备份成 .bak 文件）。</p>
<p class="small" style="color:#b91c1c">⚠️ 脚本里含该账号的登录凭证，等于账号钥匙，只在你自己的电脑上用，不要外传。</p>
{warn_html}
<p><button onclick="copyScript()">📋 一键复制脚本</button>
<a class="btn gray" href="/script-download?id={esc(aid)}">下载 .cmd 文件</a></p>
<textarea id="cmdtext" rows="16" readonly>{esc(text)}</textarea></div>
<p><a class="btn gray" href="/">返回账号列表</a></p>
<script>
async function copyScript() {{
  const t = document.getElementById('cmdtext');
  const tip = '已复制！到目标电脑新建文本文件粘贴，保存为 .cmd 后双击运行。';
  try {{ await navigator.clipboard.writeText(t.value); alert(tip); }}
  catch (e) {{ t.select(); document.execCommand('copy'); alert(tip); }}
}}
</script>"""
    return page("一键登录脚本", body)


# ---------- 对象存储（Altare U3，桶 oos） ----------
U3_BUCKET = "oos"
U3_ENV = DATA_DIR / "u3.env"
U3_MAX_UPLOAD = 5 * 1024 * 1024 * 1024  # 单文件上限 5GB（流式分片，内存恒定）
U3_PART_SIZE = 16 * 1024 * 1024         # 分片大小 16MB
U3_LINKS = DATA_DIR / "oos_links.json"  # 短链映射 {code: {key, exp}}
U3_LINK_TTL = 7 * 86400                 # 分享短链有效期 7 天
_links_lock = threading.Lock()


def _load_links() -> dict:
    try:
        data = json.loads(U3_LINKS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_links(links: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    U3_LINKS.write_text(json.dumps(links, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def oos_short_code(key: str) -> str:
    """给某个对象找一条未过期的短链代码，没有就新建（同一文件复用同一条）。"""
    now = time.time()
    with _links_lock:
        links = _load_links()
        links = {c: v for c, v in links.items() if float(v.get("exp") or 0) > now}
        for code, v in links.items():
            if v.get("key") == key:
                _save_links(links)
                return code
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        while code in links:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
        links[code] = {"key": key, "exp": now + U3_LINK_TTL}
        _save_links(links)
        return code


def oos_short_target(code: str) -> str | None:
    with _links_lock:
        v = _load_links().get(code) or {}
    if float(v.get("exp") or 0) > time.time():
        return v.get("key") or None
    return None


def oos_log(ip: str, what: str, key: str, nbytes: int) -> None:
    try:
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {what} "
                f"{key} {nbytes}\n")
        with open(DATA_DIR / "oos_access.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001  日志失败不影响下载
        pass


def u3_client():
    if u3store is None:
        return None
    return u3store.load_client(U3_ENV)


def _fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _fmt_time(iso: str) -> str:
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def oos_page(msg: str = "", is_err: bool = False) -> bytes:
    cli = u3_client()
    head = """<h1>对象存储 · Altare U3</h1>
<p><a class="btn gray" href="/">返回账号列表</a></p>"""
    if cli is None:
        return page("对象存储", head + """<div class="card">对象存储凭据未配置
（数据目录缺少 u3.env），暂时不可用。</div>""", msg, is_err)
    try:
        objs = cli.list_objects(U3_BUCKET)
    except Exception as e:  # noqa: BLE001
        return page("对象存储", head + f"""<div class="card">读取桶失败：{esc(e)}</div>""",
                    msg, is_err)
    total = sum(o["size"] for o in objs)
    rows = []
    for o in objs:
        k = esc(o["key"])
        ku = urllib.parse.quote(o["key"], safe="")
        rows.append(
            f"<tr><td style='word-break:break-all'>{k}</td>"
            f"<td>{_fmt_size(o['size'])}</td><td>{esc(_fmt_time(o['modified']))}</td>"
            f"<td style='white-space:nowrap'>"
            f"<a class='btn' href='/oos/get?key={ku}'>下载</a> "
            f"<a class='btn gray' href='/oos/link?key={ku}'>分享链接</a> "
            f"<form class='inline' method='post' action='/oos/delete' "
            f"onsubmit=\"return confirm('确定删除 {k} ？')\">"
            f"<input type='hidden' name='key' value='{k}'>"
            f"<button class='red'>删除</button></form></td></tr>")
    body = head + f"""
<div class="card small">桶 <b>{esc(U3_BUCKET)}</b>（US 区）· 共 {len(objs)} 个文件 ·
已用 {_fmt_size(total)} / 10 GB（免费额度）· 桶为私有：下载经本站中转，
分享的是 7 天有效的短链</div>
<div class="card"><h2>上传文件</h2>
<form id="oosup" method="post" action="/oos/upload" enctype="multipart/form-data">
<label>目录前缀（可空，如 img/2026 ，文件会存到该目录下）</label>
<input name="prefix" placeholder="">
<input type="file" name="file" multiple required>
<button type="submit">上传</button>
<span class="small">单个文件最大 5GB（流式分片上传，不占服务器内存）</span>
<div id="oosprog" style="display:none;margin-top:8px">
<progress id="oosbar" max="100" value="0" style="width:100%"></progress>
<div class="small" id="oostext"></div></div></form></div>
<div class="card"><h2>文件列表</h2>
<input id="oosfilter" placeholder="输入关键字过滤…" oninput="oosFilter(this.value)">
<table><tr><th>文件名</th><th>大小</th><th>修改时间</th><th>操作</th></tr>
{''.join(rows) if rows else '<tr><td colspan="4">（空桶，上传第一个文件吧）</td></tr>'}
</table></div>
<script>
function oosFilter(v) {{
  v = v.toLowerCase();
  document.querySelectorAll('table tr').forEach((tr, i) => {{
    if (i === 0) return;
    tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';
  }});
}}
const oosForm = document.getElementById('oosup');
if (oosForm) oosForm.addEventListener('submit', (e) => {{
  e.preventDefault();
  const box = document.getElementById('oosprog');
  const bar = document.getElementById('oosbar');
  const txt = document.getElementById('oostext');
  box.style.display = 'block';
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/oos/upload');
  xhr.upload.onprogress = (ev) => {{
    if (ev.lengthComputable) {{
      const pct = Math.round(ev.loaded * 100 / ev.total);
      bar.value = pct;
      txt.textContent = '正在上传：' + pct + '%（' +
        (ev.loaded / 1048576).toFixed(1) + ' / ' + (ev.total / 1048576).toFixed(1) +
        ' MB）— 大文件要几分钟，别关页面';
    }}
  }};
  xhr.onload = () => {{ location.href = xhr.responseURL || '/oos'; }};
  xhr.onerror = () => {{ txt.textContent = '上传中断，请重试'; }};
  xhr.send(new FormData(oosForm));
}});
</script>"""
    return page("对象存储", body, msg, is_err)


def oos_link_page(key: str) -> bytes:
    if not key:
        return page("分享链接", "<h1>分享链接</h1><div class='card'>不可用</div>")
    code = oos_short_code(key)
    # 域名写死正式入口：他有时从沙盒直达地址打开面板，用 Host 头拼链接会拼出
    # se-sto11.tryaltare.ai:端口 这种外部打不开的地址。
    url = f"https://koi.ipv64.de/oos/s/{code}"
    body = f"""<h1>分享链接（7 天有效）</h1>
<p><a class="btn gray" href="/oos">返回对象存储</a></p>
<div class="card"><p>文件：<b>{esc(key)}</b></p>
<textarea id="oosurl" rows="3" readonly>{esc(url)}</textarea>
<button onclick="oosCopy()">复制链接</button>
<p class="small">任何人拿到这条短链都能在 7 天内下载该文件（不用登录），过期自动失效；
同一文件重复生成的是同一条链接。</p></div>
<script>
async function oosCopy() {{
  const t = document.getElementById('oosurl');
  try {{ await navigator.clipboard.writeText(t.value); alert('已复制'); }}
  catch (e) {{ t.select(); document.execCommand('copy'); alert('已复制'); }}
}}
</script>"""
    return page("分享链接", body)


class _BodyReader:
    """按 Content-Length 计量的请求体缓冲读取器（流式 multipart 解析用）。"""

    def __init__(self, rfile, total: int):
        self.rfile = rfile
        self.left = total
        self.buf = bytearray()

    def _fill_more(self) -> bool:
        if self.left <= 0:
            return False
        chunk = self.rfile.read(min(256 * 1024, self.left))
        if not chunk:
            self.left = 0
            return False
        self.left -= len(chunk)
        self.buf += chunk
        return True

    def read_until(self, needle: bytes, sink=None):
        """消费到 needle 之前的内容：sink 给定时喂给 sink 并返回 b''，
        否则收集后返回；找不到（EOF）返回 None。"""
        out = bytearray()
        keep = len(needle) - 1
        while True:
            idx = self.buf.find(needle)
            if idx >= 0:
                data = bytes(self.buf[:idx])
                del self.buf[:idx + len(needle)]
                if sink:
                    sink(data)
                else:
                    out += data
                return bytes(out)
            if len(self.buf) > keep:
                data = bytes(self.buf[:len(self.buf) - keep])
                del self.buf[:len(self.buf) - keep]
                if sink:
                    sink(data)
                else:
                    out += data
            if not self._fill_more():
                return None

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n and self._fill_more():
            pass
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def drain(self) -> None:
        try:
            while self.left > 0:
                chunk = self.rfile.read(min(256 * 1024, self.left))
                if not chunk:
                    break
                self.left -= len(chunk)
        except Exception:  # noqa: BLE001
            pass


class _SoftUploadErr(Exception):
    """单个文件的软失败：记下原因，继续处理同批其它文件。"""


class _PartSink:
    """把一个文件的内容流式切成 16MB 分片，直传 U3 分片上传接口。"""

    def __init__(self, cli, key: str, ctype: str):
        self.cli = cli
        self.key = key
        self.ctype = ctype or ""
        self.buf = bytearray()
        self.parts: list[tuple[int, str]] = []
        self.total = 0
        self.uid: str | None = None
        self.err = ""

    def feed(self, data: bytes) -> None:
        if self.err:
            return
        self.total += len(data)
        if self.total > U3_MAX_UPLOAD:
            self.err = "超过 5GB 上限"
            return
        self.buf += data
        while len(self.buf) >= U3_PART_SIZE:
            chunk = bytes(self.buf[:U3_PART_SIZE])
            del self.buf[:U3_PART_SIZE]
            self._send(chunk)

    def _send(self, chunk: bytes) -> None:
        if self.uid is None:
            self.uid = self.cli.mp_create(U3_BUCKET, self.key, self.ctype)
        etag = self.cli.mp_upload_part(U3_BUCKET, self.key, self.uid,
                                       len(self.parts) + 1, chunk)
        self.parts.append((len(self.parts) + 1, etag))

    def finish(self) -> None:
        if self.err:
            if self.uid:
                self.cli.mp_abort(U3_BUCKET, self.key, self.uid)
            raise _SoftUploadErr(self.err)
        try:
            if self.total == 0:
                self.cli.put_object(U3_BUCKET, self.key, b"", self.ctype)
                return
            if self.buf:
                self._send(bytes(self.buf))
                self.buf.clear()
            self.cli.mp_complete(U3_BUCKET, self.key, self.uid, self.parts)
        except Exception:
            if self.uid:
                self.cli.mp_abort(U3_BUCKET, self.key, self.uid)
            raise


def _stream_upload(reader: _BodyReader, boundary: bytes, cli):
    """流式解析 multipart 请求体，逐个文件直传 U3。返回 (done, errs)。"""
    import re as _re
    first = reader.read_until(b"\r\n")
    if first is None or not first.startswith(b"--" + boundary):
        raise ValueError("multipart 起始边界不对")
    needle = b"\r\n--" + boundary
    prefix = ""
    done: list[str] = []
    errs: list[str] = []
    while True:
        head = reader.read_until(b"\r\n\r\n")
        if head is None:
            break
        htext = head.decode("utf-8", "replace")
        disp = _re.search(r"Content-Disposition:[^\r\n]*", htext)
        d = disp.group(0) if disp else ""
        name_m = _re.search(r'name="([^"]*)"', d)
        fn_m = _re.search(r'filename="([^"]*)"', d)
        ct_m = _re.search(r"Content-Type:\s*([^\r\n]+)", htext)
        ctype = ct_m.group(1).strip() if ct_m else ""
        field = name_m.group(1) if name_m else ""
        if fn_m is None:
            val = reader.read_until(needle)
            if field == "prefix" and val is not None:
                prefix = val.decode("utf-8", "replace").strip().strip("/")
        elif fn_m.group(1) == "":
            reader.read_until(needle, lambda _d: None)  # 空文件框，丢弃
        else:
            name = fn_m.group(1).replace("\\", "/").split("/")[-1].strip()
            key = f"{prefix}/{name}" if prefix else name
            sink = _PartSink(cli, key, ctype)
            reader.read_until(needle, sink.feed)
            try:
                sink.finish()
                done.append(key)
            except _SoftUploadErr as e:
                errs.append(f"{name}：{e}")
        tail = reader.read_exact(2)
        if tail == b"--":
            break
        if tail != b"\r\n":
            break
    return done, errs



# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "CBAMWeb/1.0"

    def log_message(self, *_args):  # 静音默认访问日志
        pass

    # -- 基础设施 --
    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, msg: str = "", is_err: bool = False,
                  cookie: str | None = None):
        if msg:
            to += ("&" if "?" in to else "?") + "msg=" + urllib.parse.quote(msg)
            if is_err:
                to += "&err=1"
        extra = {"Location": to}
        if cookie:
            extra["Set-Cookie"] = cookie
        self._send(303, b"", extra=extra)

    def _cookie_token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            part = part.strip()
            if part.startswith("cbw_session="):
                return part.split("=", 1)[1]
        return None

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", errors="replace") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _authed(self) -> bool:
        return session_ok(self._cookie_token())

    def _oos_serve(self, key_or_code: str, public: bool):
        """把对象内容经本站流式中转出去（下载地址始终是本站域名）。"""
        ip = (self.headers.get("X-Forwarded-For") or
              self.client_address[0]).split(",")[0].strip()
        if public:
            key = oos_short_target(key_or_code.strip())
            if not key:
                oos_log(ip, "MISS", key_or_code.strip(), 0)
                return self._send(404, page("链接不存在",
                                            "<h1>链接不存在或已过期</h1>"))
        else:
            key = key_or_code
        cli = u3_client()
        if cli is None:
            return self._send(503, page("不可用", "<h1>对象存储未配置</h1>"))
        try:
            status, headers, resp = cli.open_get(
                U3_BUCKET, key, self.headers.get("Range"))
        except Exception as e:  # noqa: BLE001
            if public:
                return self._send(404, page("链接不存在", "<h1>文件不存在</h1>"))
            return self._redirect("/oos", f"下载失败：{e}", True)
        name = key.rsplit("/", 1)[-1] or "download"
        ctype = headers.get("Content-Type") or "application/octet-stream"
        # 公开分享的音视频/图片走 inline，浏览器直接在线播放；其余仍是下载。
        inline = public and ctype.split(";")[0].strip().startswith(
            ("audio/", "video/", "image/"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        clen = headers.get("Content-Length")
        if clen:
            self.send_header("Content-Length", clen)
        crange = headers.get("Content-Range")
        if crange:
            self.send_header("Content-Range", crange)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment")
                         + "; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        sent = 0
        try:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()
            oos_log(ip, "SHORT" if public else "GET", key, sent)

    def _oos_upload(self):
        conf = load_conf()
        if not conf.get("pw_hash"):
            return self._redirect("/setup")
        if not self._authed():
            return self._redirect("/login")
        cli = u3_client()
        if cli is None:
            return self._redirect("/oos", "对象存储凭据未配置", True)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._redirect("/oos", "没有收到文件", True)
        if n > U3_MAX_UPLOAD + 4 * 1024 * 1024:
            return self._redirect("/oos", "文件太大（单文件上限 5GB）", True)
        import re as _re
        m = _re.search(r"boundary=(.+)", self.headers.get("Content-Type") or "")
        if not m:
            return self._redirect("/oos", "上传格式不对", True)
        boundary = m.group(1).strip().strip('"').encode()
        reader = _BodyReader(self.rfile, n)
        try:
            done, errs = _stream_upload(reader, boundary, cli)
        except Exception as e:  # noqa: BLE001
            reader.drain()
            return self._redirect("/oos", f"上传失败：{e}", True)
        if not done and not errs:
            return self._redirect("/oos", "没有可上传的文件", True)
        msg = f"已上传 {len(done)} 个文件" if done else ""
        if errs:
            msg = (msg + "；" if msg else "") + "失败：" + "；".join(errs)
        return self._redirect("/oos", msg, bool(errs and not done))

    def do_HEAD(self):
        """短链的 HEAD 探测（播放器开播前常先发）：只回头、不回体。"""
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/oos/s/"):
            key = oos_short_target(u.path[len("/oos/s/"):].strip())
            cli = u3_client() if key else None
            if key and cli is not None:
                try:
                    headers = cli.open_head(U3_BUCKET, key)
                except Exception:  # noqa: BLE001
                    headers = None
                if headers is not None:
                    ctype = (headers.get("Content-Type")
                             or "application/octet-stream")
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    if headers.get("Content-Length"):
                        self.send_header("Content-Length",
                                         headers["Content-Length"])
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- 路由 --
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        msg, is_err = q.get("msg", ""), q.get("err") == "1"
        conf = load_conf()

        if u.path == "/setup":
            if conf.get("pw_hash"):
                return self._redirect("/login")
            return self._send(200, setup_page(q.get("t", ""), msg, is_err))
        if not conf.get("pw_hash"):
            return self._redirect("/setup")
        if u.path == "/login":
            if self._authed():
                return self._redirect("/")
            return self._send(200, login_page(msg, is_err))
        if u.path.startswith("/oos/s/"):
            return self._oos_serve(u.path[len("/oos/s/"):], public=True)
        if not self._authed():
            return self._redirect("/login")

        if u.path == "/":
            return self._send(200, dashboard(msg, is_err))
        if u.path == "/import":
            return self._send(200, import_page(msg, is_err))
        if u.path == "/login-online":
            return self._send(200, online_login_page(q.get("k", "")))
        if u.path == "/settings":
            return self._send(200, settings_page(msg, is_err))
        if u.path == "/logs":
            return self._send(200, logs_page())
        if u.path == "/ledger":
            return self._send(200, ledger_page(q.get("account", ""),
                                               q.get("source", "")))
        if u.path == "/oos":
            return self._send(200, oos_page(msg, is_err))
        if u.path == "/oos/get":
            key = q.get("key", "")
            if not key:
                return self._redirect("/oos", "下载不可用", True)
            return self._oos_serve(key, public=False)
        if u.path == "/oos/link":
            return self._send(200, oos_link_page(q.get("key", "")))
        if u.path == "/jobstatus":
            data = {"running": bool(_job_state.get("running")),
                    "name": _job_state.get("name") or "",
                    "elapsed": int(time.time() - _job_state["started"])
                    if _job_state.get("running") else 0,
                    "last_title": _last_run.get("title") or "",
                    "last_at": _last_run.get("at") or ""}
            return self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/script":
            return self._send(200, script_page(q.get("id", "")))
        if u.path == "/script-download":
            aid = q.get("id", "")
            cached = _scripts.get(aid)
            if cached and time.time() - cached[0] < 600:
                _, text, fname = cached
            else:
                got = build_script(aid)
                if got is None or not got[0]:
                    return self._redirect("/", "生成脚本失败", True)
                text, fname = got[0], got[1]
                _scripts[aid] = (time.time(), text, fname)
            return self._send(
                200, text.encode("utf-8"), "application/octet-stream",
                {"Content-Disposition":
                 f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}"})
        return self._send(404, page("404", "<h1>页面不存在</h1>"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/oos/upload":
            return self._oos_upload()
        form = self._form()
        conf = load_conf()

        if u.path == "/setup":
            if conf.get("pw_hash"):
                return self._redirect("/login")
            if not conf.get("setup_token") or \
                    not secrets.compare_digest(form.get("token", ""), conf["setup_token"]):
                return self._send(200, setup_page(form.get("token", ""),
                                                  "初始化令牌不正确", True))
            pw = form.get("password", "")
            if len(pw) < 6:
                return self._send(200, setup_page(form.get("token", ""),
                                                  "密码至少 6 位", True))
            set_password(conf, pw)
            conf["setup_token"] = ""
            save_conf(conf)
            tok = new_session()
            return self._redirect("/", "密码已设置，欢迎使用",
                                  cookie=f"cbw_session={tok}; Path=/; HttpOnly; "
                                         f"SameSite=Lax; Max-Age={30 * 86400}")
        if u.path == "/login":
            ip = self.client_address[0]
            fails = [t for t in _login_fails.get(ip, []) if t > time.time() - 300]
            if len(fails) >= 8:
                return self._send(200, login_page("尝试次数过多，请 5 分钟后再试", True))
            if check_password(conf, form.get("password", "")):
                _login_fails.pop(ip, None)
                tok = new_session()
                return self._redirect("/", cookie=f"cbw_session={tok}; Path=/; "
                                                  f"HttpOnly; SameSite=Lax; "
                                                  f"Max-Age={30 * 86400}")
            fails.append(time.time())
            _login_fails[ip] = fails
            return self._send(200, login_page("密码不正确", True))
        if not conf.get("pw_hash"):
            return self._redirect("/setup")
        if not self._authed():
            return self._redirect("/login")

        store = AccountStore()
        if u.path == "/logout":
            _sessions.pop(self._cookie_token() or "", None)
            return self._redirect("/login", cookie="cbw_session=; Path=/; Max-Age=0")
        if u.path == "/run/full":
            run_job_async("手动：续期+额度+签到+活跃+补偿+成长计划", job_full)
            return self._redirect("/", "已开始执行（续期+额度+签到+活跃+补偿+成长计划），稍等片刻刷新页面看结果")
        if u.path == "/run/quota-all":
            run_job_async("手动：刷新全部额度", job_quota_all)
            return self._redirect("/", "已开始刷新全部账号额度，稍等片刻刷新页面看结果")
        if u.path == "/login-online/start":
            key = start_online_login()
            return self._redirect(f"/login-online?k={key}")
        if u.path == "/run/growth":
            run_job_async("手动：成长计划",
                          lambda: _fmt_results(
                              "成长计划", cb_app.growth_all(AccountStore())))
            return self._redirect("/", "已开始执行成长计划，稍等片刻刷新页面看结果")
        if u.path == "/run/status":
            run_job_async("手动：签到状态查询", job_status)
            return self._redirect("/", "已开始查询签到状态，稍等片刻刷新页面看结果")
        if u.path in ("/run/checkin", "/run/refresh", "/run/quota", "/run/liveness"):
            kind = u.path.rsplit("/", 1)[1]
            aid = form.get("id", "")
            run_job_async(f"手动：单账号{kind}", lambda: job_single(kind, aid))
            return self._redirect("/", "已开始执行，稍等片刻刷新页面看结果")
        if u.path == "/account/delete":
            with RUN_LOCK:
                ok = store.remove(form.get("id", ""))
            return self._redirect("/", "已删除" if ok else "账号不存在", not ok)
        if u.path == "/oos/delete":
            cli = u3_client()
            key = form.get("key", "")
            if cli is None or not key:
                return self._redirect("/oos", "删除不可用", True)
            try:
                cli.delete_object(U3_BUCKET, key)
            except Exception as e:  # noqa: BLE001
                return self._redirect("/oos", f"删除失败：{e}", True)
            return self._redirect("/oos", f"已删除 {key}")
        if u.path == "/import/paste":
            text = form.get("text", "")
            if not text.strip():
                return self._redirect("/import", "内容为空", True)
            try:
                recs = parse_pasted_text(text)
            except Exception as e:  # noqa: BLE001
                return self._redirect("/import", f"导入失败：{e}", True)
            with RUN_LOCK:
                for rec in recs:
                    store.add(rec)
            return self._redirect("/", f"已导入 {len(recs)} 个账号")
        if u.path == "/import/cloud":
            pw = form.get("passphrase", "").strip() or saved_cloud_passphrase(conf)
            if not pw:
                return self._redirect("/import", "请填写同步码", True)
            ref = form.get("ref", "").strip() or None
            with RUN_LOCK:
                ok, text = cb_app.cloud_pull(store, pw, ref)
            return self._redirect("/" if ok else "/import", text, not ok)
        if u.path == "/cloud/push":
            pw = saved_cloud_passphrase(conf)
            if not pw:
                return self._redirect("/settings",
                                      "还没保存同步码，先在设置里填一次", True)
            with RUN_LOCK:
                ok, text = cb_app.cloud_push(store, pw)
            return self._redirect("/settings", text, not ok)
        if u.path == "/settings":
            conf["endpoint"] = form.get("endpoint", "").strip()
            dt = form.get("daily_time", "09:05").strip()
            conf["daily_time"] = dt if len(dt) == 5 and dt[2] == ":" else "09:05"
            try:
                conf["growth_every_hours"] = max(0, min(24, int(
                    form.get("growth_every_hours", "2"))))
            except ValueError:
                conf["growth_every_hours"] = 2
            if form.get("cloud_passphrase", "").strip():
                save_cloud_passphrase(conf, form["cloud_passphrase"].strip())
            if form.get("new_password", ""):
                if len(form["new_password"]) < 6:
                    return self._redirect("/settings", "新密码至少 6 位", True)
                set_password(conf, form["new_password"])
            save_conf(conf)
            apply_endpoint(conf)
            return self._redirect("/settings", "设置已保存")
        return self._send(404, page("404", "<h1>页面不存在</h1>"))


def main():
    conf = load_conf()
    if not conf.get("pw_hash") and not conf.get("setup_token"):
        conf["setup_token"] = secrets.token_urlsafe(24)
        save_conf(conf)
    apply_endpoint(load_conf())
    if load_conf().get("setup_token"):
        print(f"[cbam-web] 首次设置令牌：{load_conf()['setup_token']}", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[cbam-web] listening on 0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
