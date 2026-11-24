# -*- coding: utf-8 -*-
"""网页版 wb_target 替身：把登录态写进本机 WorkBuddy 是桌面版专属功能，
网页版不提供（cb_app.sync_to_workbuddy 只会被桌面流程调用，这里明确返回跳过）。
"""
from __future__ import annotations


def sync_session(_plain, extra_account=None):
    return False, "网页版不支持同步到本机 WorkBuddy", None


def status_line() -> str:
    return "WorkBuddy 同步：网页版不可用"


def current_account():
    return None
