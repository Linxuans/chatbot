# -*- coding: utf-8 -*-
"""微信智能体控制台（Tkinter）。

三个作用：
1. 看状态：网关是否在跑、微信是否登录、是否暂停、当前模型
2. 一键操作：启动 / 停止 / 暂停 / 恢复 / 重启
3. 改配置：把 .env 里的参数做成表单，保存后可直接重启生效
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .config import PROJECT_ROOT
from .envfile import EnvFile
from .system import (
    gateway_pid,
    gateway_running,
    is_paused,
    open_path,
    set_paused,
    start_gateway,
    stop_gateway,
    tail_log,
    wechat_state,
)

ENV_PATH = PROJECT_ROOT / ".env"
LOG_PATH = PROJECT_ROOT / "logs" / "gui.log"


def _setup_logging() -> logging.Logger:
    """pythonw 下 sys.stderr 是 None，任何异常输出都会二次抛错把进程带走。

    所以把日志固定落到 logs/gui.log，并把 stderr 也指向它。
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if sys.stderr is None:
        sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    logger = logging.getLogger("wxagent.gui")
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = _setup_logging()

# (分组, [(键, 标签, 类型, 可选值)])
SCHEMA: list[tuple[str, list[tuple]]] = [
    ("模型与联网", [
        ("DEEPSEEK_API_KEY", "API Key", "secret", None),
        ("DEEPSEEK_BASE_URL", "接口地址", "str", None),
        ("DEEPSEEK_MODEL", "模型", "choice", ["deepseek-flash", "deepseek-v4-pro"]),
        ("TEMPERATURE", "温度（0~2，越高越发散）", "float", None),
        ("MAX_TOKENS", "单次最大输出 token", "int", None),
        ("ENABLE_WEB_SEARCH", "允许联网搜索", "bool", None),
    ]),
    ("机器人身份", [
        ("BOT_NAMES", "机器人昵称（多个用逗号分隔）", "str", None),
        ("TRIGGER_MODE", "触发方式", "choice", ["at", "prefix", "all"]),
        ("TRIGGER_PREFIX", "触发前缀（prefix 模式用）", "str", None),
        ("REPLY_IN_PRIVATE", "私聊也回复", "bool", None),
        ("EXTRA_SYSTEM_PROMPT", "额外人设 / 群规", "text", None),
    ]),
    ("上下文与记忆", [
        ("CONTEXT_MESSAGES", "每次带多少条群消息", "int", None),
        ("SUMMARY_TRIGGER", "攒多少条压缩摘要", "int", None),
        ("SUMMARY_KEEP", "摘要压缩后保留条数", "int", None),
        ("SUMMARY_MAX_CHARS", "摘要字数上限", "int", None),
    ]),
    ("防刷限制", [
        ("COOLDOWN_SECONDS", "同群冷却（秒）", "float", None),
        ("MAX_REPLIES_PER_MINUTE", "每分钟最多回复条数", "int", None),
        ("MAX_REPLY_CHARS", "单条消息字数上限", "int", None),
    ]),
    ("群与权限", [
        ("ALLOWED_ROOMS", "只服务这些群（留空=全部）", "str", None),
        ("BLOCKED_ROOMS", "永不回复的群", "str", None),
        ("ADMIN_WXIDS", "允许 /reset 的 wxid", "str", None),
    ]),
    ("运行时", [
        ("UIA_POLL_INTERVAL", "轮询间隔（秒）", "float", None),
        ("LOG_LEVEL", "日志级别", "choice", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ]),
]

PAD = 8

# .env 里可能不存在某些键（老版本配置文件），表单要给默认值，
# 否则输入框是空的，点保存就会弹「需要数字」。
DEFAULTS: dict[str, str] = {
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-flash",
    "TEMPERATURE": "0.7",
    "MAX_TOKENS": "400",
    "ENABLE_WEB_SEARCH": "true",
    "TRIGGER_MODE": "at",
    "TRIGGER_PREFIX": "/",
    "REPLY_IN_PRIVATE": "true",
    "CONTEXT_MESSAGES": "6",
    "SUMMARY_TRIGGER": "40",
    "SUMMARY_KEEP": "12",
    "SUMMARY_MAX_CHARS": "400",
    "COOLDOWN_SECONDS": "1",
    "MAX_REPLIES_PER_MINUTE": "20",
    "MAX_REPLY_CHARS": "1800",
    "UIA_POLL_INTERVAL": "3",
    "LOG_LEVEL": "INFO",
}


def wechat_label(state: str) -> str:
    return f"微信：{state}"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"微信智能体控制台  v{__version__}")
        self.geometry("1120x720")
        self.minsize(1000, 600)

        self.env = EnvFile(ENV_PATH)
        self.vars: dict[str, tk.Variable] = {}
        self.auto_log = tk.BooleanVar(value=True)

        self._build_style()
        self._build_header()
        self._build_statusbar()   # 先占住底部，否则会被 notebook 的 expand 挤掉
        self._build_tabs()

        self.refresh_status()
        self.refresh_log()
        self.report_callback_exception = self._on_callback_error
        self.after(2000, self._tick)

    def _on_callback_error(self, exc_type, exc_value, exc_tb) -> None:
        """Tk 回调里的异常不能让界面崩掉，只记日志。"""
        log.error("界面回调异常: %s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    # ---------------------------------------------------------------- 外观
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Ok.TLabel", foreground="#1f7a1f")
        style.configure("Bad.TLabel", foreground="#b00020")
        style.configure("Warn.TLabel", foreground="#a06000")

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(PAD * 2, PAD))
        header.pack(fill="x")
        ttk.Label(header, text="微信智能体控制台", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"v{__version__}", style="Status.TLabel").pack(side="left", padx=10)

        # 状态不放这里：Tk 的右对齐在同一行里容易把文字挤出窗口，
        # 改为放进控制页的「运行信息」区域（左对齐、全宽、稳定）


    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        self.control_tab = ttk.Frame(notebook, padding=PAD)
        self.settings_tab = ttk.Frame(notebook, padding=PAD)
        notebook.add(self.control_tab, text="  控制  ")
        notebook.add(self.settings_tab, text="  设置  ")
        self.notebook = notebook
        self._build_control(self.control_tab)
        self._build_settings(self.settings_tab)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(PAD * 2, 4))
        bar.pack(fill="x", side="bottom")
        self.lbl_hint = ttk.Label(bar, text="", style="Status.TLabel")
        self.lbl_hint.pack(side="left")

    # ---------------------------------------------------------------- 控制页
    def _build_control(self, parent: ttk.Frame) -> None:
        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=(0, PAD))

        self.btn_start = ttk.Button(buttons, text="启动网关", command=self.on_start, width=12)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(buttons, text="停止网关", command=self.on_stop, width=12)
        self.btn_stop.pack(side="left", padx=6)
        self.btn_pause = ttk.Button(buttons, text="暂停", command=self.on_pause, width=10)
        self.btn_pause.pack(side="left", padx=6)
        self.btn_resume = ttk.Button(buttons, text="恢复", command=self.on_resume, width=10)
        self.btn_resume.pack(side="left", padx=6)
        ttk.Button(buttons, text="重启网关", command=self.on_restart, width=12).pack(side="left", padx=6)
        ttk.Button(buttons, text="打开日志文件", command=self.on_open_log, width=14).pack(side="left", padx=6)

        info = ttk.LabelFrame(parent, text="运行信息", padding=PAD)
        info.pack(fill="x", pady=(0, PAD))
        self.lbl_info = ttk.Label(info, text="", style="Status.TLabel", justify="left")
        self.lbl_info.pack(anchor="w")

        log_frame = ttk.LabelFrame(parent, text="最近日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(log_frame)
        toolbar.pack(fill="x")
        ttk.Checkbutton(toolbar, text="自动刷新（2 秒）", variable=self.auto_log).pack(side="left")
        ttk.Button(toolbar, text="立即刷新", command=self.refresh_log).pack(side="right")

        self.log_text = ScrolledText(log_frame, height=16, wrap="none", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------------- 设置页
    def _build_settings(self, parent: ttk.Frame) -> None:
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="both", expand=True)

        canvas = tk.Canvas(wrapper, highlightthickness=0)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for group, fields in SCHEMA:
            frame = ttk.LabelFrame(inner, text=group, padding=PAD)
            frame.pack(fill="x", pady=(0, PAD), padx=(0, PAD))
            frame.columnconfigure(1, weight=1)
            for row, (key, label, kind, options) in enumerate(fields):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
                self._build_field(frame, row, key, kind, options)

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(PAD, 0))
        ttk.Button(actions, text="保存", command=self.on_save, width=12).pack(side="left")
        ttk.Button(actions, text="保存并重启网关", command=self.on_save_restart, width=20).pack(side="left", padx=8)
        ttk.Button(actions, text="放弃修改", command=self.reload_settings, width=12).pack(side="left")

    def _build_field(self, frame: ttk.Frame, row: int, key: str, kind: str, options) -> None:
        current = self.env.get(key, "") or DEFAULTS.get(key, "")
        if kind == "bool":
            var = tk.BooleanVar(value=current.strip().lower() in {"1", "true", "yes", "on"})
            ttk.Checkbutton(frame, variable=var).grid(row=row, column=1, sticky="w")
        elif kind == "choice":
            var = tk.StringVar(value=current or (options[0] if options else ""))
            combo = ttk.Combobox(frame, textvariable=var, values=options, state="readonly", width=28)
            combo.grid(row=row, column=1, sticky="w")
        elif kind == "text":
            var = tk.StringVar(value=current)
            entry = ttk.Entry(frame, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew")
        elif kind == "secret":
            var = tk.StringVar(value=current)
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=1, sticky="ew")
            entry = ttk.Entry(holder, textvariable=var, show="*")
            entry.pack(side="left", fill="x", expand=True)
            ttk.Checkbutton(
                holder, text="显示", command=lambda e=entry: e.configure(show="")
                if e.cget("show") else e.configure(show="*")
            ).pack(side="left", padx=6)
        else:
            var = tk.StringVar(value=current)
            entry = ttk.Entry(frame, textvariable=var, width=30)
            entry.grid(row=row, column=1, sticky="w")
        self.vars[key] = var

    def reload_settings(self) -> None:
        self.env = EnvFile(ENV_PATH)
        for _group, fields in SCHEMA:
            for key, _label, kind, _options in fields:
                value = self.env.get(key, "") or DEFAULTS.get(key, "")
                var = self.vars.get(key)
                if var is None:
                    continue
                if kind == "bool":
                    var.set(value.strip().lower() in {"1", "true", "yes", "on"})
                else:
                    var.set(value)
        self.set_hint("已重新加载 .env")

    # ---------------------------------------------------------------- 状态
    def refresh_status(self) -> None:
        try:
            self._refresh_status_inner()
        except Exception:
            log.exception("刷新状态失败")

    def _refresh_status_inner(self) -> None:
        running = gateway_running()
        pid = gateway_pid()
        paused = is_paused()
        wechat = wechat_state()

        gateway_text = (f"运行中（PID {pid}）" if pid else "运行中") if running else "已停止"
        # 机器人状态必须由「进程是否在跑」推导，不能只看暂停标志，
        # 否则停止网关后仍显示「监听中」，看着像没停
        if not running:
            bot_text = "已离线"
        elif paused:
            bot_text = "已暂停"
        else:
            bot_text = "监听中"

        model = self.env.get("DEEPSEEK_MODEL", "-")
        search = "开" if self.env.get("ENABLE_WEB_SEARCH", "true").lower() in {"1", "true", "yes", "on"} else "关"
        self.lbl_info.configure(
            text=(
                f"网关：{gateway_text}    微信：{wechat}    机器人：{bot_text}\n"
                f"模型：{model}    联网搜索：{search}    "
                f"触发方式：{self.env.get('TRIGGER_MODE', 'at')}\n"
                f"冷却：{self.env.get('COOLDOWN_SECONDS', '-')} 秒    "
                f"每分钟上限：{self.env.get('MAX_REPLIES_PER_MINUTE', '-')} 条    "
                f"上下文：{self.env.get('CONTEXT_MESSAGES', '-')} 条"
            ),
            style="Ok.TLabel" if (running and not paused and wechat == "已登录") else "Warn.TLabel",
        )

        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_pause.configure(state="normal" if (running and not paused) else "disabled")
        self.btn_resume.configure(state="normal" if (running and paused) else "disabled")

    def refresh_log(self) -> None:
        try:
            self._refresh_log_inner()
        except Exception:
            log.exception("刷新日志失败")

    def _refresh_log_inner(self) -> None:
        content = tail_log(200)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", content)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tick(self) -> None:
        self.refresh_status()
        if self.auto_log.get():
            self.refresh_log()
        self.report_callback_exception = self._on_callback_error
        self.after(2000, self._tick)

    def _on_callback_error(self, exc_type, exc_value, exc_tb) -> None:
        """Tk 回调里的异常不能让界面崩掉，只记日志。"""
        log.error("界面回调异常: %s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def set_hint(self, text: str) -> None:
        self.lbl_hint.configure(text=text)
        self.after(7000, lambda: self.lbl_hint.configure(text=f"Ctrl+R 刷新    {ENV_PATH}"))

    # ---------------------------------------------------------------- 动作
    def on_start(self) -> None:
        ok, message = start_gateway()
        (self.set_hint if ok else lambda t: messagebox.showerror("启动失败", t))(message)
        self.refresh_status()

    def on_stop(self) -> None:
        if not messagebox.askyesno("确认", "停止网关后机器人将离线，确定吗？"):
            return
        ok, message = stop_gateway()
        (self.set_hint if ok else lambda t: messagebox.showerror("停止失败", t))(message)
        self.refresh_status()

    def on_pause(self) -> None:
        set_paused(True)
        self.set_hint("已暂停：网关不再碰微信窗口，可以安全切账号/关窗口")
        self.refresh_status()

    def on_resume(self) -> None:
        set_paused(False)
        self.set_hint("已恢复监听")
        self.refresh_status()

    def on_restart(self) -> None:
        stop_gateway()
        ok, message = start_gateway()
        (self.set_hint if ok else lambda t: messagebox.showerror("重启失败", t))(message)
        self.refresh_status()

    def on_open_log(self) -> None:
        path = PROJECT_ROOT / "logs" / "bot.log"
        if not path.is_file():
            messagebox.showinfo("提示", "日志还没生成")
            return
        open_path(path)

    def _validate(self) -> list[str]:
        errors: list[str] = []
        for _group, fields in SCHEMA:
            for key, label, kind, _options in fields:
                var = self.vars.get(key)
                if var is None:
                    continue
                raw = str(var.get()).strip()
                if not raw:
                    continue  # 留空视为使用默认值
                if kind == "int":
                    try:
                        int(raw)
                    except ValueError:
                        errors.append(f"{label} 需要整数（当前填的是「{raw}」）")
                elif kind == "float":
                    try:
                        float(raw)
                    except ValueError:
                        errors.append(f"{label} 需要数字（当前填的是「{raw}」）")
        return errors

    def _write_env(self) -> bool:
        errors = self._validate()
        if errors:
            messagebox.showerror("参数有误", "\n".join(errors))
            return False
        for _group, fields in SCHEMA:
            for key, _label, kind, _options in fields:
                var = self.vars.get(key)
                if var is None:
                    continue
                if kind == "bool":
                    self.env.set(key, "true" if var.get() else "false")
                else:
                    value = str(var.get()).strip() or DEFAULTS.get(key, "")
                    self.env.set(key, value)
        try:
            self.env.save()
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return False
        self.env = EnvFile(ENV_PATH)
        self.refresh_status()
        return True

    def on_save(self) -> None:
        if self._write_env():
            self.set_hint("配置已保存；重启网关后生效")

    def on_save_restart(self) -> None:
        if not self._write_env():
            return
        was_running = gateway_pid() is not None
        if was_running:
            stop_gateway()
        ok, message = start_gateway()
        if ok:
            self.set_hint("配置已保存，网关已重启")
        else:
            messagebox.showerror("重启失败", message)
        self.refresh_status()


def main() -> int:
    log.info("控制台启动 v%s（python=%s）", __version__, sys.executable)
    app = App()
    app.bind("<Control-r>", lambda _e: (app.refresh_status(), app.refresh_log()))
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())