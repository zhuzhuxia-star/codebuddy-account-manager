# -*- coding: utf-8 -*-
"""每日自动签到：把 `checkin-all` 注册成 Windows 计划任务。

不引入第三方依赖，直接用系统自带的 `schtasks`（无需管理员权限，当前用户任务）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "CodeBuddyAccountManagerDailyCheckin"
STORE_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
LOG_FILE = STORE_DIR / "checkin.log"

_HERE = Path(__file__).resolve().parent


def command_string() -> str:
    """计划任务实际执行的命令。

    打包成 exe 后（sys.frozen）用 exe 自身；源码运行时用当前解释器 + main.py，
    这样计划任务执行的始终是"当前这份代码"，不会因为 dist 里的旧 exe 而失效。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" checkin-all'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")  # 无窗口，避免每天弹黑框
    if pyw.exists():
        py = pyw
    return f'"{py}" "{_HERE / "main.py"}" checkin-all'


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def install(hour: int = 9, minute: int = 5) -> tuple[bool, str]:
    """注册每日签到任务，返回 (是否成功, 说明)。"""
    r = _run(["/Create", "/TN", TASK_NAME, "/TR", command_string(),
              "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}", "/F"])
    if r.returncode == 0:
        return True, f"已注册每日 {hour:02d}:{minute:02d} 自动签到\n{command_string()}"
    return False, (r.stderr or r.stdout or "schtasks 执行失败").strip()[:300]


def uninstall() -> tuple[bool, str]:
    """删除自动签到任务。"""
    r = _run(["/Delete", "/TN", TASK_NAME, "/F"])
    if r.returncode == 0:
        return True, "已取消每日自动签到"
    return False, (r.stderr or r.stdout or "schtasks 执行失败").strip()[:300]


def is_installed() -> bool:
    return _run(["/Query", "/TN", TASK_NAME]).returncode == 0


# ---------- 开机自启（登录后触发签到） ----------
# 说明：schtasks 的 /SC ONLOGON 在本机需要管理员权限（实测"拒绝访问"），
# 因此改用 HKCU 的 Run 项 —— 同样在登录时触发，且当前用户即可设置。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CodeBuddyAccountManagerCheckin"


def install_autostart(delay_minutes: int = 1) -> tuple[bool, str]:
    """设置"开机（登录）后自动签到"：写入 HKCU\\...\\Run，无需管理员权限。

    delay_minutes 仅用于提示（Run 项本身不支持延迟），实际在登录后立即执行一次
    `checkin-all`；配合每日定时任务可覆盖长时间开机的场景。
    """
    import winreg

    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command_string())
        finally:
            winreg.CloseKey(key)
    except OSError as e:
        return False, f"写入开机自启失败：{e}"
    return True, ("已设置开机（登录）后自动签到（HKCU\\...\\Run）\n"
                  f"{command_string()}\n提示：登录后约 {max(0, int(delay_minutes))} 分钟内会完成签到")


def uninstall_autostart() -> tuple[bool, str]:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return True, "开机自启签到未设置"
    try:
        winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        return True, "开机自启签到未设置"
    except OSError as e:
        return False, f"取消开机自启失败：{e}"
    finally:
        winreg.CloseKey(key)
    return True, "已取消开机自启签到"


def is_autostart_installed() -> bool:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def describe() -> str:
    daily = "已开启" if is_installed() else "未开启"
    auto = "已开启" if is_autostart_installed() else "未开启"
    return f"自动任务（先续期再签到）：开机自启 {auto} / 每日定时 {daily}"


def append_log(lines: list[str]) -> None:
    """把一次自动签到的结果追加到日志（便于计划任务场景排查）。"""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
