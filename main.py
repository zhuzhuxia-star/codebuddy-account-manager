# -*- coding: utf-8 -*-
"""CodeBuddy 账号管理器入口（作者：这是哪头猪？）。

仓库：https://github.com/zhuzhuxia-star/codebuddy-account-manager

用法：
    python main.py              # 启动 GUI
    python main.py version      # 显示版本与作者信息
    python main.py list         # 列出账号
    python main.py ide-account  # 显示当前 IDE 生效账号
    python main.py checkin-all  # 全部账号：先续期登录态，再签到（计划任务用；--no-refresh 跳过续期）
    python main.py refresh-all  # 只续期全部账号的登录态
    python main.py checkin-status   # 只查询各账号签到状态（不领取）
    python main.py wb-status    # 显示 WorkBuddy 目标与当前账号
    python main.py wb-sync      # 把当前 IDE 登录态同步到 WorkBuddy
    python main.py cloud-status          # 云端账号库状态
    python main.py cloud-genpass         # 生成/显示同步码（跨电脑用的钥匙）
    python main.py cloud-setpass <同步码> # 在新电脑上设置同步码
    python main.py cloud-push [口令]     # 账号库加密上传（跨电脑保存）
    python main.py cloud-pull [口令] [slug|URL]  # 从云端取回并合并
    python main.py install-task [HH:MM]  # 注册每日定时签到
    python main.py uninstall-task        # 取消每日定时签到
    python main.py install-autostart [延迟分钟]  # 设置开机（登录）后自动签到
    python main.py uninstall-autostart           # 取消开机自启签到
"""
from __future__ import annotations

import json
import sys
import time


def _utf8_console():
    """Windows 控制台默认 GBK，输出账号名/中文时避免 UnicodeEncodeError。

    打包成 --windowed 后没有控制台，sys.stdout 为 None，此处补一个黑洞流，
    否则计划任务（checkin-all）里的 print 会抛 AttributeError。
    """
    import io
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, io.UnsupportedOperation):
            pass


def cmd_version():
    import meta
    print(f"{meta.APP_NAME} v{meta.VERSION}")
    print(f"作者：{meta.AUTHOR}")
    print(f"仓库：{meta.GITHUB_URL}")
    return 0


def cmd_list():
    from account_store import AccountStore
    for a in AccountStore().list_accounts():
        print(json.dumps(a, ensure_ascii=False, indent=2))


def cmd_ide_account():
    import cb_app
    print(cb_app.current_ide_account())


def cmd_checkin_all():
    """每日任务：先把全部账号登录态续期，再调用签到。

    续期失败不影响签到（旧 access token 若仍有效照样能签）；加 `--no-refresh` 可跳过续期。
    """
    import cb_app
    import cb_task
    from account_store import AccountStore

    store = AccountStore()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"==== 每日续期+签到 {stamp} ===="]

    if "--no-refresh" not in sys.argv:
        print("---- 续期登录态 ----")
        lines.append("-- 续期 --")
        for label, ok, text in cb_app.refresh_all_sessions(store):
            line = f"[{'OK  ' if ok else 'FAIL'}] {label} | {text}"
            print(line)
            lines.append(line)

    print("---- 每日签到 ----")
    lines.append("-- 签到 --")
    results = cb_app.checkin_all(store)
    for label, ok, text in results:
        line = f"[{'OK  ' if ok else 'FAIL'}] {label} | {text}"
        print(line)
        lines.append(line)
    ok_n = sum(1 for _, ok, _ in results if ok)
    tail = f"完成 {ok_n}/{len(results)}"
    print(tail)
    lines.append(tail)
    cb_task.append_log(lines)
    return 0 if ok_n == len(results) else 1


def cmd_refresh_all():
    """只续期登录态（不签到）。"""
    import cb_app
    from account_store import AccountStore

    results = cb_app.refresh_all_sessions(AccountStore())
    for label, ok, text in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {label} | {text}")
    ok_n = sum(1 for _, ok, _ in results if ok)
    print(f"完成 {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) else 1


def cmd_checkin_status():
    import cb_app
    from account_store import AccountStore

    store = AccountStore()
    for rec in store.list_accounts():
        ok, text = cb_app.refresh_checkin_status(store, rec["id"])
        print(f"[{'OK  ' if ok else 'FAIL'}] {rec.get('label')} | {text}")
    return 0


def cmd_wb_status():
    import wb_target
    print(wb_target.status_line())
    print("WorkBuddy 当前账号：", wb_target.current_account() or "（无法读取）")
    return 0


def cmd_wb_sync():
    import cb_app
    from account_store import AccountStore

    store = AccountStore()
    try:
        acct = cb_app.import_from_ide(store)
    except Exception as e:  # noqa: BLE001
        print(f"读取当前 IDE 登录态失败：{e}")
        return 1
    ok, msg = cb_app.sync_to_workbuddy(acct)
    print(f"[{'OK  ' if ok else 'FAIL'}] {acct.get('label')} | {msg}")
    return 0 if ok else 1


def _passphrase(idx: int = 2) -> str:
    """同步口令来源：命令行参数 > 环境变量 CBAC_PASSPHRASE > 本机记住的口令。"""
    import os

    if len(sys.argv) > idx and sys.argv[idx].strip():
        return sys.argv[idx].strip()
    env = os.environ.get("CBAC_PASSPHRASE", "").strip()
    if env:
        return env
    import cb_cloud
    return cb_cloud.saved_passphrase()


def cmd_cloud_status():
    import cb_app
    import cb_cloud

    print(cb_app.cloud_status())
    print("配置文件：", cb_cloud.CONF_FILE)
    print("存在：", cb_cloud.CONF_FILE.exists(), "| 目录：", cb_cloud.STORE_DIR)
    return 0


def cmd_cloud_genpass():
    """生成（或显示本机已保存的）同步码。"""
    import cb_cloud
    p = cb_cloud.saved_passphrase() or cb_cloud.ensure_passphrase()
    print("同步码：", p)
    print("（已保存在本机；换电脑时在新机器执行：cloud-setpass <同步码>）")
    return 0


def cmd_cloud_setpass():
    import cb_cloud
    if len(sys.argv) < 3:
        print("用法：cloud-setpass <同步码>")
        return 2
    cb_cloud.save_passphrase(sys.argv[2])
    print("[OK  ] 已把同步码保存到本机")
    return 0


def cmd_cloud_push():
    import cb_app
    import cb_cloud
    from account_store import AccountStore

    passphrase = _passphrase()
    if not passphrase:
        print("缺少同步口令：请传参（cloud-push <口令>）或设置环境变量 CBAC_PASSPHRASE")
        return 2
    ok, msg = cb_app.cloud_push(AccountStore(), passphrase)
    print(("[OK  ] " if ok else "[FAIL] ") + msg)
    print("地址：", cb_cloud.vault_url())
    return 0 if ok else 1


def cmd_cloud_pull():
    import cb_app
    from account_store import AccountStore

    passphrase = _passphrase()
    if not passphrase:
        print("缺少同步口令：请传参（cloud-pull <口令> [文章slug或URL]）")
        return 2
    ref = sys.argv[3] if len(sys.argv) > 3 else None
    ok, msg = cb_app.cloud_pull(AccountStore(), passphrase, ref)
    print(("[OK  ] " if ok else "[FAIL] ") + msg)
    return 0 if ok else 1


def cmd_install_autostart():
    import cb_task
    delay = 1
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        delay = int(sys.argv[2])
    ok, msg = cb_task.install_autostart(delay)
    print(msg)
    return 0 if ok else 1


def cmd_uninstall_autostart():
    import cb_task
    ok, msg = cb_task.uninstall_autostart()
    print(msg)
    return 0 if ok else 1


def cmd_install_task():
    import cb_task
    arg = sys.argv[2] if len(sys.argv) > 2 else "09:05"
    try:
        hh, mm = (int(x) for x in arg.split(":", 1))
    except ValueError:
        print(f"时间格式应为 HH:MM，收到：{arg}")
        return 2
    ok, msg = cb_task.install(hh, mm)
    print(msg)
    return 0 if ok else 1


def cmd_uninstall_task():
    import cb_task
    ok, msg = cb_task.uninstall()
    print(msg)
    return 0 if ok else 1


def main():
    if len(sys.argv) > 1:
        c = sys.argv[1]
        table = {
            "version": cmd_version,
            "about": cmd_version,
            "list": cmd_list,
            "ide-account": cmd_ide_account,
            "checkin-all": cmd_checkin_all,
            "refresh-all": cmd_refresh_all,
            "checkin-status": cmd_checkin_status,
            "wb-status": cmd_wb_status,
            "wb-sync": cmd_wb_sync,
            "cloud-status": cmd_cloud_status,
            "cloud-genpass": cmd_cloud_genpass,
            "cloud-setpass": cmd_cloud_setpass,
            "cloud-push": cmd_cloud_push,
            "cloud-pull": cmd_cloud_pull,
            "install-task": cmd_install_task,
            "uninstall-task": cmd_uninstall_task,
            "install-autostart": cmd_install_autostart,
            "uninstall-autostart": cmd_uninstall_autostart,
        }
        fn = table.get(c)
        if fn is None:
            print(f"未知命令: {c}")
            return 2
        return fn() or 0
    from gui import main as gui_main
    gui_main()
    return 0


def _log_crash(exc: BaseException):
    """GUI 崩溃兜底：把 traceback 写到账号库目录下的 error.log 便于排查。"""
    import os
    import traceback
    from pathlib import Path
    try:
        err = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager" / "error.log"
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _utf8_console()
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        _log_crash(e)
        raise
