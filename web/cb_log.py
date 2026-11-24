# -*- coding: utf-8 -*-
"""统一日志：接口请求、签到、切号等都会写到这里，方便排查"卡住/慢"的问题。

- 落盘：`%APPDATA%\\CodeBuddyAccountManager\\app.log`（超过 2MB 自动滚动为 app.log.1）
- 内存：保留最近若干行，供 GUI「查看日志」窗口实时展示
- 控制台：源码运行时同步打印（打包成 --windowed 后没有控制台，自动跳过）
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

LOG_DIR = Path(os.environ.get("APPDATA", "")) / "CodeBuddyAccountManager"
LOG_FILE = LOG_DIR / "app.log"
MAX_BYTES = 2 * 1024 * 1024
MAX_MEM = 1000

_buf: deque[str] = deque(maxlen=MAX_MEM)
_lock = threading.Lock()
_echo = True


def set_echo(enabled: bool) -> None:
    """是否把日志同时打印到控制台（GUI 下建议关闭）。"""
    global _echo
    _echo = bool(enabled)


def _roll() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
    except Exception:
        pass


def log(msg: str, tag: str = "app") -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{tag}] {msg}"
    with _lock:
        _buf.append(line)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _roll()
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    if _echo:
        try:
            if sys.stdout is not None:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        except Exception:
            pass


def _tail_file(n: int, max_bytes: int = 262144) -> list[str]:
    """从日志文件尾部读最近 n 行（只读尾部若干 KB，避免大文件整读）。"""
    try:
        if not LOG_FILE.exists():
            return []
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢弃可能被截断的半行
            return [ln for ln in (s.rstrip("\n") for s in f) if ln.strip()][-n:]
    except Exception:
        return []


def tail(n: int = 300) -> list[str]:
    """最近 n 行：以日志文件为准（这样界面一打开就能看到历史），文件不可读时退回内存。"""
    lines = _tail_file(n)
    if lines:
        return lines
    with _lock:
        return list(_buf)[-n:]


def path() -> Path:
    return LOG_FILE
