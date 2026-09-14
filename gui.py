# -*- coding: utf-8 -*-
"""CodeBuddy 账号管理工具 GUI（tkinter）。

线程约定：所有网络/加解密等耗时操作放在后台线程，结果通过 queue 交回主线程，
由主线程轮询更新界面（tkinter 只能在主线程操作）。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import cb_app
import cb_cloud
import cb_log
import cb_runtime
import cb_task
import meta
import wb_api
import wb_target
from account_store import AccountStore, parse_pasted_text, quota_columns


def checkin_column(a: dict) -> str:
    """列表「签到」列展示文本（取账号库里缓存的今日结果）。"""
    c = a.get("checkin") or {}
    if not c:
        return "-"
    if c.get("date") != time.strftime("%Y-%m-%d"):
        return "未签到"
    if c.get("status") in (wb_api.CLAIMED, wb_api.ALREADY_CLAIMED):
        credit = c.get("credit") or 0
        return f"已签到 +{credit}" if credit else "已签到"
    return c.get("label") or "-"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.store = AccountStore()
        self._busy = False
        root.title(f"{meta.APP_NAME} v{meta.VERSION} — {meta.AUTHOR}")
        root.geometry("1180x780")
        root.minsize(980, 620)

        self._build_topbar()
        self._build_table()
        self._build_log_panel()
        self._build_statusbar()
        self.refresh()

    # ---------- 线程辅助 ----------
    @staticmethod
    def run_background(task):
        """后台执行 task()，返回 Queue，结果以 ("ok", value) / ("err", exc) 投递。"""
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                q.put(("ok", task()))
            except BaseException as e:  # noqa: BLE001
                q.put(("err", e))

        threading.Thread(target=worker, daemon=True).start()
        return q

    @staticmethod
    def poll(widget, q: queue.Queue, on_done, interval: int = 120):
        """在主线程轮询队列，拿到结果后回调 on_done(value, error)。"""
        def step():
            try:
                alive = bool(widget.winfo_exists())
            except Exception:
                alive = False
            if not alive:
                return
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                widget.after(interval, step)
                return
            if kind == "ok":
                on_done(payload, None)
            else:
                on_done(None, payload)

        widget.after(interval, step)

    # ---------- UI ----------
    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 2))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bar, text="登录新账号", command=self.login_new).pack(side=tk.LEFT)
        ttk.Button(bar, text="从当前 IDE 导入", command=self.import_from_ide).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="粘贴导入登录态", command=self.paste_import).pack(side=tk.LEFT, padx=(6, 0))

        self.run_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="切号时自动结束并重开 CodeBuddy",
            variable=self.run_var).pack(side=tk.RIGHT)

        # 第二行：选中/批量操作
        bar2 = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        bar2.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar2, text="全选", command=self.select_all).pack(side=tk.LEFT)
        ttk.Button(bar2, text="取消选择", command=self.clear_selection).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar2, text="刷新列表", command=self.refresh).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(bar2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.switch_btn = ttk.Button(bar2, text="切换到选中账号",
                                     command=self.switch_selected)
        self.switch_btn.pack(side=tk.LEFT)
        ttk.Button(bar2, text="刷新额度", command=self.refresh_quota).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar2, text="续期登录态", command=self.refresh_session).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar2, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT, padx=(6, 0))
        self.sel_label = ttk.Label(bar2, text="未选择账号")
        self.sel_label.pack(side=tk.RIGHT)

        # 第三行：WorkBuddy 同步 / 云端同步 / 签到
        bar3 = ttk.Frame(self.root, padding=(10, 2, 10, 2))
        bar3.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar3, text="立即签到", command=self.checkin_selected).pack(side=tk.LEFT)
        ttk.Button(bar3, text="查询签到状态",
                   command=self.checkin_status_selected).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar3, text="成长计划",
                   command=self.growth_selected).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(bar3, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(bar3, text="同步到 WorkBuddy",
                   command=self.sync_workbuddy).pack(side=tk.LEFT)
        ttk.Button(bar3, text="云端同步…",
                   command=self.cloud_dialog).pack(side=tk.LEFT, padx=(6, 0))
        self.log_btn = ttk.Button(bar3, text="折叠日志", command=self.toggle_log_panel)
        self.log_btn.pack(side=tk.RIGHT)
        ttk.Button(bar3, text=f"GitHub · {meta.AUTHOR}",
                   command=lambda: webbrowser.open(meta.GITHUB_URL)
                   ).pack(side=tk.RIGHT, padx=(0, 6))

        # 第四行：自动化开关
        bar4 = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        bar4.pack(side=tk.TOP, fill=tk.X)
        self.auto_btn = ttk.Button(bar4, text="开机自启签到", command=self.toggle_autostart)
        self.auto_btn.pack(side=tk.LEFT)
        self.task_btn = ttk.Button(bar4, text="每日定时签到", command=self.toggle_task)
        self.task_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(bar4, text="执行频率：").pack(side=tk.LEFT, padx=(10, 0))
        self.freq_var = tk.StringVar()
        self.freq_box = ttk.Combobox(bar4, textvariable=self.freq_var, width=11,
                                     state="readonly",
                                     values=list(cb_task.SCHEDULE_LABELS))
        self.freq_box.pack(side=tk.LEFT, padx=(2, 0))
        self.freq_box.bind("<<ComboboxSelected>>", self._on_freq_changed)
        self._load_freq_selection()
        self.cloud_label = ttk.Label(bar4, text="")
        self.cloud_label.pack(side=tk.RIGHT)
        self._update_task_btn()

    def _build_table(self):
        wrap = ttk.Frame(self.root, padding=(10, 0))
        wrap.pack(fill=tk.BOTH, expand=True)

        cols = ("label", "uid", "email", "plan", "quota", "checkin", "source", "created")
        headers = ("账号", "uid", "手机/邮箱", "套餐", "额度", "签到", "来源", "加入时间")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="extended")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=130, anchor=tk.W)
        self.tree.column("label", width=160)
        self.tree.column("uid", width=230)
        self.tree.column("email", width=110)
        self.tree.column("plan", width=80)
        self.tree.column("quota", width=230)
        self.tree.column("checkin", width=130)
        self.tree.column("source", width=90)
        self.tree.column("created", width=130)

        ys = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", lambda e: self.switch_selected())
        self.tree.bind("<Control-a>", lambda e: self.select_all() or "break")
        self.tree.bind("<Control-A>", lambda e: self.select_all() or "break")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_sel_count())

        bottom = ttk.Frame(self.root, padding=(10, 6))
        bottom.pack(fill=tk.X)
        self.ide_label = ttk.Label(bottom, text="")
        self.ide_label.pack(side=tk.LEFT)
        self.sel_label2 = ttk.Label(bottom, text="")
        self.sel_label2.pack(side=tk.RIGHT)
        # 作者与仓库地址（点击打开）
        repo_link = ttk.Label(bottom, text=meta.GITHUB_URL, foreground="#0a66c2",
                              cursor="hand2")
        repo_link.pack(side=tk.RIGHT, padx=(0, 16))
        repo_link.bind("<Button-1>", lambda e: webbrowser.open(meta.GITHUB_URL))

    def _build_statusbar(self):
        self.status = ttk.Label(self.root, anchor=tk.W, relief=tk.SUNKEN, padding=(8, 3))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    def _set_status(self, msg: str):
        self.status.config(text=msg)
        self.root.update_idletasks()

    def _update_sel_count(self):
        n = len(self.tree.selection())
        total = len(self.store.list_accounts())
        self.sel_label.config(text=f"已选 {n}/{total}")
        self.sel_label2.config(text=f"已选 {n}/{total} 个账号")

    # ---------- 数据 ----------
    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for a in self.store.list_accounts():
            plan, quota = quota_columns(a)
            self.tree.insert("", tk.END, iid=a["id"], values=(
                a.get("label", ""), a.get("uid", ""), a.get("email", ""),
                plan, quota, checkin_column(a),
                a.get("source", ""), a.get("created_at", "")))

        cur = cb_app.current_ide_account()
        running = "正在运行" if cb_runtime.is_running() else "未运行"
        wbrun = "正在运行" if cb_runtime.is_workbuddy_running() else "未运行"
        ide_txt = (f"IDE 当前账号：{cur or '（无法读取）'}  |  CodeBuddy：{running}"
                   f"  |  WorkBuddy：{wbrun}")
        self.ide_label.config(text=ide_txt)
        try:
            self.cloud_label.config(text=cb_app.cloud_status())
        except Exception:
            pass
        self._update_sel_count()
        self._set_status(f"共 {len(self.store.list_accounts())} 个账号。"
                         "可多选（Ctrl/Shift/全选）后批量刷新额度、续期或删除。"
                         "切号会替换 CodeBuddy 加密登录态，原登录态自动备份。")

    def select_all(self):
        for i in self.tree.get_children():
            self.tree.selection_add(i)
        self._update_sel_count()

    def clear_selection(self):
        self.tree.selection_remove(*self.tree.selection())
        self._update_sel_count()

    def _selected_accounts(self) -> list[dict] | None:
        sels = self.tree.selection()
        if not sels:
            messagebox.showinfo("提示", "请先在列表中选择账号（可 Ctrl/Shift 多选或点“全选”）")
            return None
        out = [self.store.get(i) for i in sels]
        return [a for a in out if a]

    # ---------- 批量任务 ----------
    def _batch_run(self, accounts, work, wait_hint, on_finished):
        """后台依次对每个账号执行 work(account)->(ok, text)，期间汇报进度。

        消息协议（务必保持二元组，step 按 `kind, payload` 解包）：
          ("p", (第几个, 总数, 账号名))  进度
          ("d", results)                 全部完成
        """
        if self._busy:
            messagebox.showinfo("提示", "有操作正在进行，请稍候")
            return
        self._busy = True
        self.root.configure(cursor="watch")
        q: queue.Queue = queue.Queue()
        t_start = time.time()
        state = {"text": f"{wait_hint}…"}
        total = len(accounts)

        def runner():
            results = []
            for i, a in enumerate(accounts):
                q.put(("p", (i + 1, total, a.get("label", ""))))
                try:
                    ok, text = work(a)
                except BaseException as e:  # noqa: BLE001
                    ok, text = False, str(e)
                cb_log.log(f"{wait_hint} | {a.get('label')} | "
                           f"{'OK' if ok else 'FAIL'} | {text}", "batch")
                results.append((a, ok, text))
            q.put(("d", results))

        threading.Thread(target=runner, daemon=True).start()

        def finish(results):
            if state.get("done"):
                return  # 幂等：避免异常路径重复收尾/重复弹窗
            state["done"] = True
            self._busy = False
            try:
                self.root.configure(cursor="")
                self._batch_summary(results, wait_hint)
                on_finished()
            except BaseException as e:  # noqa: BLE001
                # 例如任务结束时用户已关闭主窗口，此时忽略即可
                cb_log.log(f"批量任务收尾异常：{type(e).__name__}: {e}", "batch")

        def tick():
            """等待期间刷新已用时，让界面看起来"在动"，不是卡死。"""
            if self._busy:
                self._set_status(f"{state['text']} · 已 {int(time.time() - t_start)}s")
                self.root.after(200, tick)

        def step():
            if not self._busy:
                return
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                self.root.after(120, step)
                return
            except BaseException as e:  # noqa: BLE001
                cb_log.log(f"批量任务消息异常：{type(e).__name__}: {e}", "batch")
                finish([])
                return
            try:
                if kind == "p":
                    i, n, label = payload
                    state["text"] = f"{wait_hint}（{i}/{n}）：{label}"
                    self._set_status(f"{state['text']} · 已 {int(time.time() - t_start)}s")
                    self.root.after(120, step)
                else:
                    finish(payload)
            except BaseException as e:  # noqa: BLE001
                # 任何解析/回调异常都必须复位界面，否则会永远转圈
                cb_log.log(f"批量任务处理异常：{type(e).__name__}: {e}", "batch")
                finish(payload if isinstance(payload, list) else [])

        self.root.after(120, tick)
        self.root.after(120, step)

    def _batch_summary(self, results, title):
        ok_n = sum(1 for _, ok, _ in results if ok)
        win = tk.Toplevel(self.root)
        win.title(f"{title} - 完成 {ok_n}/{len(results)}")
        win.geometry("780x420")
        win.transient(self.root)

        frm = ttk.Frame(win, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=f"成功 {ok_n} / 失败 {len(results) - ok_n}").pack(anchor=tk.W)
        txt = tk.Text(frm, font=("Consolas", 10))
        ys = ttk.Scrollbar(frm, orient=tk.VERTICAL, command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        for a, ok, text in results:
            mark = "OK" if ok else "FAIL"
            txt.insert(tk.END, f"[{mark}] {a.get('label')}\n    {text}\n")
        txt.config(state=tk.DISABLED)
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=6)
        if not ok_n:
            messagebox.showwarning(title, "全部失败，详见明细", parent=win)

    # ---------- 导入 ----------
    def import_from_ide(self):
        try:
            account = cb_app.import_from_ide(self.store)
        except Exception as e:
            messagebox.showerror("导入失败", str(e))
            return
        self.store.add(account)
        messagebox.showinfo("导入成功", f"已导入：{account.get('label')}")
        self.refresh()

    def paste_import(self):
        win = tk.Toplevel(self.root)
        win.title("粘贴登录态 JSON")
        win.geometry("640x360")
        txt = tk.Text(win, wrap=tk.WORD, font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        txt.insert("1.0", "在此粘贴 CodeBuddy 账号的登录态 JSON（对象或数组均可，"
                          "支持 auth_raw / access_token / IDE 会话明文等格式）...")

        def do_import():
            content = txt.get("1.0", tk.END).strip()
            try:
                accounts = parse_pasted_text(content)
            except Exception as e:
                messagebox.showerror("解析失败", str(e), parent=win)
                return
            names = []
            for acct in accounts:
                self.store.add(acct)
                names.append(acct.get("label", ""))
            win.destroy()
            messagebox.showinfo("导入成功", f"已导入 {len(accounts)} 个账号：\n"
                                            + "\n".join(names))
            self.refresh()

        ttk.Button(win, text="导入", command=do_import).pack(pady=(0, 8))

    # ---------- 软件内登录 ----------
    def login_new(self):
        win = tk.Toplevel(self.root)
        win.title("登录 CodeBuddy 账号")
        win.geometry("620x300")
        win.transient(self.root)

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        state = {"stop": False}
        ui_q: queue.Queue = queue.Queue()

        status_var = tk.StringVar(value="正在获取登录地址…")
        ttk.Label(frm, textvariable=status_var, wraplength=580,
                  justify=tk.LEFT).pack(anchor=tk.W)

        pb = ttk.Progressbar(frm, mode="indeterminate", length=580)
        pb.pack(fill=tk.X, pady=(10, 6))
        pb.start(12)

        url_var = tk.StringVar(value="")
        url_row = ttk.Frame(frm)
        url_row.pack(fill=tk.X)
        ttk.Entry(url_row, textvariable=url_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(url_row, text="复制",
                   command=lambda: self._copy(url_var.get())).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(url_row, text="打开浏览器",
                   command=lambda: url_var.get() and webbrowser.open(url_var.get())
                   ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(frm, foreground="#666", wraplength=580, justify=tk.LEFT,
                  text="流程与 IDE 登录一致：在浏览器完成登录后，本窗口会自动获取 token 与"
                       "账号信息并保存到账号库（不会写入 IDE，需再点“切换到选中账号”）。"
                  ).pack(anchor=tk.W, pady=(10, 0))

        btn_row = ttk.Frame(frm)
        btn_row.pack(side=tk.BOTTOM, pady=(12, 0))
        cancel_btn = ttk.Button(btn_row, text="取消", command=lambda: cancel())
        cancel_btn.pack(side=tk.RIGHT)

        def cancel():
            state["stop"] = True
            win.destroy()

        def drain_ui():
            while True:
                try:
                    kind, payload = ui_q.get_nowait()
                except queue.Empty:
                    break
                if kind == "url":
                    url_var.set(payload)
                    webbrowser.open(payload)
                elif kind == "status":
                    status_var.set(payload)
            if win.winfo_exists():
                win.after(150, drain_ui)

        win.after(150, drain_ui)

        def done(result, error):
            if not win.winfo_exists():
                return
            pb.stop()
            cancel_btn.configure(state=tk.DISABLED)
            if error is not None:
                if "取消" in str(error):
                    return
                status_var.set(f"登录失败：{error}")
                messagebox.showerror("登录失败", str(error), parent=win)
                return
            rec = result
            self.store.add(rec)
            self.refresh()
            messagebox.showinfo("登录成功",
                                f"已保存：{rec.get('label')}\n"
                                "现在可在列表中选中它并点击“切换到选中账号”。", parent=win)
            win.destroy()

        task_q = self.run_background(lambda: cb_app.login_account(
            on_auth_url=lambda u: ui_q.put(("url", u)),
            on_status=lambda t: ui_q.put(("status", t)),
            should_stop=lambda: state["stop"]))
        self.poll(win, task_q, done, interval=200)

    @staticmethod
    def _copy(text: str):
        if not text:
            return
        try:
            root = tk._default_root
            if root:
                root.clipboard_clear()
                root.clipboard_append(text)
        except Exception:
            pass

    # ---------- 批量：额度 / 续期 / 删除 ----------
    def refresh_quota(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._batch_run(
            accounts,
            lambda a: cb_app.refresh_quota(self.store, a["id"]),
            "正在刷新额度",
            lambda: self.refresh())

    def refresh_session(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._batch_run(
            accounts,
            lambda a: (lambda r: (r.ok, r.message))(cb_app.refresh_session(self.store, a["id"])),
            "正在续期登录态",
            lambda: self.refresh())

    def delete_selected(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        names = "\n".join(" - " + a.get("label", "") for a in accounts)
        if not messagebox.askyesno(
                "确认删除",
                f"删除所选 {len(accounts)} 个账号？\n{names}\n\n"
                "（仅从工具列表移除，不影响 CodeBuddy 当前登录态）"):
            return
        for a in accounts:
            self.store.remove(a["id"])
        self.refresh()

    # ---------- WorkBuddy 签到 / 同步 ----------
    def checkin_selected(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._batch_run(accounts,
                        lambda a: cb_app.checkin_account(self.store, a["id"]),
                        "正在调用 WorkBuddy 每日签到", lambda: self.refresh())

    def checkin_status_selected(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._batch_run(accounts,
                        lambda a: cb_app.refresh_checkin_status(self.store, a["id"]),
                        "正在查询签到状态", lambda: self.refresh())

    def growth_selected(self):
        """成长计划：接受任务/领奖/打卡兑换/抽奖（幂等，重复点只处理增量）。"""
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._batch_run(accounts,
                        lambda a: cb_app.growth_account(self.store, a["id"]),
                        "正在执行成长计划（接受任务/领奖/兑换/抽奖）",
                        lambda: self.refresh())

    def sync_workbuddy(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._set_status(wb_target.status_line())
        self._batch_run(accounts, lambda a: cb_app.sync_to_workbuddy(a),
                        "正在同步到 WorkBuddy", lambda: self.refresh())

    def toggle_task(self):
        if cb_task.is_installed():
            ok, msg = cb_task.uninstall()
        else:
            # 按下拉框当前选择的频率注册并记住
            time_str, _ = cb_task.load_schedule()
            interval = self._selected_interval()
            cb_task.save_schedule(time_str, interval)
            hh, mm = (int(x) for x in time_str.split(":", 1))
            ok, msg = cb_task.install(hh, mm, interval)
        self._update_task_btn()
        if ok:
            messagebox.showinfo("自动签到任务", msg)
        else:
            messagebox.showerror("自动签到任务", msg)
        self._set_status(cb_task.describe())

    # ---------- 执行频率 ----------
    def _load_freq_selection(self):
        _, interval = cb_task.load_schedule()
        self.freq_var.set(cb_task.schedule_label(interval))

    def _selected_interval(self) -> int:
        label = self.freq_var.get()
        for hours, text in cb_task.SCHEDULE_PRESETS:
            if text == label:
                return hours
        return 0

    def _on_freq_changed(self, _evt=None):
        """选择频率即保存；若定时任务已开启则立刻按新频率重新注册。"""
        time_str, _ = cb_task.load_schedule()
        interval = self._selected_interval()
        cb_task.save_schedule(time_str, interval)
        if not cb_task.is_installed():
            self._set_status(f"执行频率已记住：{cb_task.schedule_label(interval)}"
                             "（开启定时任务时生效）")
            return
        hh, mm = (int(x) for x in time_str.split(":", 1))
        ok, msg = cb_task.install(hh, mm, interval)
        self._update_task_btn()
        self._set_status(("已应用：" if ok else "应用失败：") + msg.split("\n")[0])

    def _update_task_btn(self):
        try:
            auto = cb_task.is_autostart_installed()
            daily = cb_task.is_installed()
        except Exception:
            auto = daily = False
        freq = cb_task.schedule_label(cb_task.load_schedule()[1])
        self.auto_btn.config(
            text="开机自启（续期+签到）：已开启" if auto else "开机自启（续期+签到）：未开启")
        self.task_btn.config(
            text=f"定时任务（续期+签到+成长计划·{freq}）："
                 + ("已开启" if daily else "未开启"))
        try:
            self.cloud_label.config(text=cb_app.cloud_status())
        except Exception:
            pass

    def toggle_autostart(self):
        if cb_task.is_autostart_installed():
            ok, msg = cb_task.uninstall_autostart()
        else:
            ok, msg = cb_task.install_autostart(1)
        self._update_task_btn()
        if ok:
            messagebox.showinfo("开机自启签到", msg)
        else:
            messagebox.showerror("开机自启签到", msg)
        self._set_status(cb_task.describe())

    # ---------- 云端同步 ----------
    def cloud_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("云端账号库同步（跨电脑）")
        win.geometry("640x340")
        win.transient(self.root)

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, wraplength=600, justify=tk.LEFT,
                  text=("账号登录态会先加密、再上传到 md.dcio.eu.org 的文章里（文章是公开页面，"
                        "所以绝不能放明文 token：那等于把账号通行证公开出去）。\n"
                        "· 直接点「生成同步码」即可，不用自己编口令；\n"
                        "· 换电脑时在新机器上粘贴同一串同步码，就能取回全部账号；\n"
                        "· 首次点「上传」创建云端文章，之后点「上传」更新同一篇。")
                  ).pack(anchor=tk.W)

        row = ttk.Frame(frm)
        row.pack(fill=tk.X, pady=(10, 4))
        ttk.Label(row, text="同步码：").pack(side=tk.LEFT)
        pw_var = tk.StringVar(value=cb_cloud.saved_passphrase())
        show_var = tk.BooleanVar(value=False)
        pw_entry = ttk.Entry(row, textvariable=pw_var, show="*")
        pw_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def gen_pass():
            p = cb_cloud.generate_passphrase()
            pw_var.set(p)
            cb_cloud.save_passphrase(p)
            try:
                self._copy(p)
            except Exception:
                pass
            messagebox.showinfo(
                "同步码已生成",
                f"已生成并复制到剪贴板：\n\n{p}\n\n"
                "本机已记住，平时不用再输；换电脑时把它粘到新机器的这个框里即可解密云端账号。",
                parent=win)

        row1 = ttk.Frame(frm)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(row1, text="生成同步码", command=gen_pass).pack(side=tk.LEFT)
        ttk.Checkbutton(row1, text="显示", variable=show_var,
                        command=lambda: pw_entry.config(
                            show="" if show_var.get() else "*")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row1, text="复制同步码",
                   command=lambda: self._copy(pw_var.get())).pack(side=tk.LEFT, padx=(6, 0))

        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X)
        ttk.Label(row2, text="文章 URL/slug（留空用本机记录）：").pack(side=tk.LEFT)
        ref_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=ref_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        status_var = tk.StringVar(value=cb_app.cloud_status())
        ttk.Label(frm, textvariable=status_var, wraplength=600, justify=tk.LEFT,
                  foreground="#444").pack(anchor=tk.W, pady=(10, 0))

        def do(action):
            pw = pw_var.get().strip()
            if not pw:
                # 零门槛：没填就自动生成一串并记住，只需在换电脑时粘贴它
                pw = cb_cloud.ensure_passphrase()
                pw_var.set(pw)
                messagebox.showinfo(
                    "已自动生成同步码",
                    f"已自动生成并记住：\n\n{pw}\n\n"
                    "换电脑时要用它才能解密云端账号，请务必保存好（已复制到剪贴板）。",
                    parent=win)
                self._copy(pw)
            ref = ref_var.get().strip() or None
            status_var.set("正在与云端通信…")
            if action == "push":
                def task():
                    return cb_app.cloud_push(self.store, pw)
            else:
                def task():
                    return cb_app.cloud_pull(self.store, pw, ref)
            q = self.run_background(task)

            def done(res, err):
                if err is not None:
                    status_var.set(f"失败：{err}")
                    messagebox.showerror("云端同步失败", str(err), parent=win)
                    return
                ok, msg = res
                status_var.set(msg)
                self._update_task_btn()
                if ok:
                    self.refresh()
                    messagebox.showinfo("云端同步", msg, parent=win)
                else:
                    messagebox.showerror("云端同步失败", msg, parent=win)

            self.poll(win, q, done, interval=200)

        btns = ttk.Frame(frm)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="上传到云端（加密）",
                   command=lambda: do("push")).pack(side=tk.LEFT)
        ttk.Button(btns, text="从云端同步",
                   command=lambda: do("pull")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns, text="复制云端地址",
                   command=lambda: self._copy(cb_cloud.vault_url())).pack(side=tk.RIGHT)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    # ---------- 日志面板（内嵌在主窗口下方） ----------
    def _build_log_panel(self):
        outer = ttk.Frame(self.root, padding=(10, 4, 10, 0))
        outer.pack(side=tk.BOTTOM, fill=tk.X)

        head = ttk.Frame(outer)
        head.pack(fill=tk.X)
        ttk.Label(head, text="运行日志（接口请求 / 批量任务 / 签到）"
                  ).pack(side=tk.LEFT)
        ttk.Button(head, text="打开日志文件夹",
                   command=lambda: self._open_folder(cb_log.path().parent)
                   ).pack(side=tk.RIGHT)
        ttk.Button(head, text="清空显示", command=self.clear_log_view
                   ).pack(side=tk.RIGHT, padx=(0, 6))

        self.log_body = ttk.Frame(outer)
        self.log_body.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        self.log_box = tk.Text(self.log_body, height=8, font=("Consolas", 9),
                               wrap=tk.NONE, state=tk.DISABLED)
        ys = ttk.Scrollbar(self.log_body, orient=tk.VERTICAL,
                           command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=ys.set)
        self.log_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)

        self._log_visible = True
        self._log_last = ""     # 已渲染的最后一行（作为增量锚点）
        self.root.after(600, self._pump_log)

    def _pump_log(self):
        """增量渲染日志：首次显示历史，之后只追加新行（不打断用户滚动）。"""
        if not self.root.winfo_exists():
            return
        try:
            if self._log_visible:
                lines = cb_log.tail(300) or []
                last = lines[-1] if lines else ""
                if last and last != self._log_last:
                    prev = self._log_last
                    if prev and prev in lines:
                        start = lines.index(prev) + 1
                        at_bottom = self.log_box.yview()[1] >= 0.999
                        self.log_box.config(state=tk.NORMAL)
                        self.log_box.insert(tk.END, "\n".join(lines[start:]) + "\n")
                        if at_bottom:
                            self.log_box.see(tk.END)
                        self.log_box.config(state=tk.DISABLED)
                    else:
                        # 首次渲染，或旧内容已被滚动窗口挤掉 → 整块重绘
                        self.log_box.config(state=tk.NORMAL)
                        self.log_box.delete("1.0", tk.END)
                        self.log_box.insert(tk.END, "\n".join(lines) + "\n")
                        self.log_box.see(tk.END)
                        self.log_box.config(state=tk.DISABLED)
                    self._log_last = last
        except Exception:
            pass
        self.root.after(1500, self._pump_log)

    def toggle_log_panel(self):
        if self._log_visible:
            self.log_body.pack_forget()
            self.log_btn.config(text="展开日志")
        else:
            self.log_body.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
            self.log_btn.config(text="折叠日志")
        self._log_visible = not self._log_visible

    def clear_log_view(self):
        """只清空显示（不动日志文件）；之后只显示新产生的行。"""
        try:
            self.log_box.config(state=tk.NORMAL)
            self.log_box.delete("1.0", tk.END)
            self.log_box.config(state=tk.DISABLED)
        except Exception:
            pass
        lines = cb_log.tail(300) or []
        self._log_last = lines[-1] if lines else ""

    @staticmethod
    def _open_folder(path):
        try:
            os.startfile(str(path))  # noqa: S606
        except Exception:
            pass

    # ---------- 切号 ----------
    def switch_selected(self):
        accounts = self._selected_accounts()
        if not accounts:
            return
        if len(accounts) > 1:
            messagebox.showinfo("提示", "切号请只选择一个账号")
            return
        a = accounts[0]
        force = self.run_var.get()
        hint = ("即将把 CodeBuddy IDE 登录态切换为：\n\n"
                f"    {a.get('label')}\n\n"
                "切换前会先备份当前登录态；完成后会把同一份登录态同步到 WorkBuddy"
                "（若 WorkBuddy 数据目录可用）。")
        if force:
            hint += "\n\n勾选了自动重启：将强制结束 CodeBuddy（未保存内容会丢失），写库后自动重新打开。"
        if not messagebox.askyesno("确认切换", hint):
            return

        res = cb_app.switch_to_account(a, force_kill=force)
        if not res.ok:
            messagebox.showerror("切号失败", res.message)
            return
        if force:
            launch_ok = cb_runtime.launch()
            res.message += "\nCodeBuddy 已重新打开。" if launch_ok else "\n未能自动启动 CodeBuddy，请手动打开。"
        messagebox.showinfo("切换完成", res.message)
        self.refresh()


def main():
    cb_log.set_echo(False)  # 界面模式下不再往控制台刷日志
    cb_log.log(f"==== 界面启动 ==== frozen={getattr(sys, 'frozen', False)} "
               f"stdout={sys.stdout is not None}")
    root = tk.Tk()
    cb_log.log("Tk root 创建成功")
    App(root)
    cb_log.log("主窗口构建完成，进入事件循环")
    root.mainloop()
    cb_log.log("事件循环已退出")
