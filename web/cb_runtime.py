# -*- coding: utf-8 -*-
"""网页版 cb_runtime 替身：服务器上没有 CodeBuddy IDE 进程。

cb_api.resolve_endpoint 只用 find_codebuddy_exe() 定位 product.json，
返回 None 时回退到 CB_API_ENDPOINT 环境变量 / 默认网关 —— 正是网页版需要的。
"""
from __future__ import annotations


def find_codebuddy_exe():
    return None


def is_running() -> bool:
    return False


def is_workbuddy_running() -> bool:
    return False


def terminate() -> None:
    return None
