# -*- coding: utf-8 -*-
"""自动签到：把 `checkin-all` 注册成 Windows 计划任务。

不引入第三方依赖，直接用系统自带的 `schtasks`（无需管理员权限，当前用户任务）。
支持两种频率：每天一次（/SC DAILY）或每 N 小时一次（/SC HOURLY /MO N）。
执行频率保存在 %APPDATA%\\CodeBuddyAccountManager\\schedule.json。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "CodeBuddyAccountManagerDailyCheckin"
STORE_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
LOG_FILE = STORE_DIR / "checkin.log"
SCHEDULE_FILE = STORE_DIR / "schedule.json"

# (间隔小时, 下拉框文案)；0 = 每天一次
SCHEDULE_PRESETS: tuple[tuple[int, str], ...] = (
    (0, "每天一次"),
    (1, "每 1 小时"),
    (2, "每 2 小时"),
    (3, "每 3 小时"),
    (4, "每 4 小时"),
    (6, "每 6 小时"),
    (8, "每 8 小时"),
    (12, "每 12 小时"),
)
SCHEDULE_LABELS: tuple[str, ...] = tuple(label for _, label in SCHEDULE_PRESETS)

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


def load_schedule() -> tuple[str, int]:
    """读已保存的执行频率，返回 (HH:MM, 间隔小时)。缺省每天 09:05。"""
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        return (str(data.get("time") or "09:05"),
                int(data.get("interval_hours") or 0))
    except Exception:  # noqa: BLE001
        return "09:05", 0


def save_schedule(time_str: str, interval_hours: int) -> None:
    """保存执行频率（时间 HH:MM + 间隔小时，0=每天）。"""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        SCHEDULE_FILE.write_text(
            json.dumps({"time": time_str, "interval_hours": interval_hours},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def schedule_label(interval_hours: int) -> str:
    """间隔小时的中文描述（与下拉框文案一致）。"""
    for h, label in SCHEDULE_PRESETS:
        if h == interval_hours:
            return label
    return f"每 {interval_hours} 小时"


def install(hour: int = 9, minute: int = 5,
            interval_hours: int = 0) -> tuple[bool, str]:
    """注册自动签到任务；interval_hours>=1 表示每 N 小时一次，0 表示每天一次。"""
    start = f"{hour:02d}:{minute:02d}"
    if interval_hours >= 1:
        args = ["/Create", "/TN", TASK_NAME, "/TR", command_string(),
                "/SC", "HOURLY", "/MO", str(interval_hours),
                "/ST", start, "/F"]
        desc = (f"已注册自动任务：从 {start} 起每 {interval_hours} 小时执行一次"
                "\n（先续期登录态 → 签到 → 成长计划，全程幂等）")
    else:
        args = ["/Create", "/TN", TASK_NAME, "/TR", command_string(),
                "/SC", "DAILY", "/ST", start, "/F"]
        desc = f"已注册每天 {start} 自动任务（先续期 → 签到 → 成长计划）"
    r = _run(args)
    if r.returncode == 0:
        return True, f"{desc}\n{command_string()}"
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
    _, interval = load_schedule()
    freq = schedule_label(interval)
    return f"自动任务（先续期再签到+成长计划）：开机自启 {auto} / 定时[{freq}] {daily}"


def append_log(lines: list[str]) -> None:
    """把一次自动签到的结果追加到日志（便于计划任务场景排查）。"""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
