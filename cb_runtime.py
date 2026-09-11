# -*- coding: utf-8 -*-
"""CodeBuddy 进程检测 / 启动。

注意：实际 IDE 分不同发行版/版本：
- 国内版进程/数据目录/主程序名为 "CodeBuddy CN"（本机现状）
- 旧版为 "CodeBuddy"
此处同时兼容两者。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROC_NAMES = ("CodeBuddy CN.exe", "CodeBuddy.exe")
# WorkBuddy 自身进程（它是 CodeBuddy 的伴侣壳，切号时也要一并退出）
WB_PROC_NAMES = ("WorkBuddy.exe", "WorkDaddy.exe")

# 可先用环境变量 CODEBUDDY_EXE 指定主程序；下面是常见安装位置（含便携安装到自定义目录）
_EXE_CANDIDATES = [
    Path(r"D:\WorkSpace\Apps\CodeBuddy CN\CodeBuddy CN\CodeBuddy CN.exe"),  # 便携安装示例，可按需删改
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "CodeBuddy CN" / "CodeBuddy CN.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "CodeBuddy CN" / "CodeBuddy CN.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "CodeBuddy CN" / "CodeBuddy CN.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "CodeBuddy" / "CodeBuddy.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "CodeBuddy" / "CodeBuddy.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "CodeBuddy" / "CodeBuddy.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codebuddy" / "CodeBuddy.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "CodeBuddy" / "CodeBuddy.exe",
]


def find_codebuddy_exe() -> Path | None:
    env = os.environ.get("CODEBUDDY_EXE", "").strip()
    if env:
        try:
            p = Path(env)
            if p.exists():
                return p
        except Exception:
            pass
    for p in _EXE_CANDIDATES:
        if p.exists():
            return p
    # 尝试 PATH 中的命令
    for name in ("CodeBuddy CN.exe", "CodeBuddy.exe", "codebuddy.exe"):
        try:
            r = subprocess.run(["where.exe", name], capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            continue
        if r.returncode == 0 and r.stdout.strip():
            p = Path(r.stdout.strip().splitlines()[0].strip())
            if p.exists():
                return p
    return None


def _running_names(names=PROC_NAMES) -> list[str]:
    running = []
    for name in names:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if name in r.stdout:
                running.append(name)
        except OSError:
            continue
    return running


def is_running() -> bool:
    return bool(_running_names())


def is_workbuddy_running() -> bool:
    return bool(_running_names(WB_PROC_NAMES))


def terminate() -> bool:
    ok = False
    for name in _running_names(PROC_NAMES + WB_PROC_NAMES):
        try:
            subprocess.run(["taskkill", "/IM", name, "/F"],
                           capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            ok = True
        except OSError:
            continue
    return ok


def launch() -> bool:
    exe = find_codebuddy_exe()
    if not exe:
        return False
    subprocess.Popen([str(exe)])
    return True
