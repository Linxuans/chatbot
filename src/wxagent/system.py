# -*- coding: utf-8 -*-
"""进程与微信状态探测（给 GUI 用）。"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from .config import PAUSE_FLAG, PID_FILE, PROJECT_ROOT

WECHAT_TITLE = ("微信", "WeChat", "Weixin")
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def is_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def gateway_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if is_alive(pid) else None


def is_paused() -> bool:
    return PAUSE_FLAG.exists()


def wechat_state() -> str:
    """返回「未运行 / 等待扫码 / 已登录」。"""
    user32 = ctypes.windll.user32
    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if (buf.value or "").strip() not in WECHAT_TITLE:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        found.append(cls.value or "")
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return "未运行"
    if any("Login" in cls for cls in found):
        return "等待扫码"
    return "已登录"


def start_gateway() -> tuple[bool, str]:
    """以无控制台方式启动网关，等待 pid 文件出现。"""
    if gateway_pid():
        return True, "网关已在运行"
    pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        return False, f"找不到 {pythonw}"

    subprocess.Popen(
        [str(pythonw), "run.py", "--mode", "uia"],
        cwd=str(PROJECT_ROOT),
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    for _ in range(40):
        if gateway_pid():
            return True, "网关已启动"
        import time
        time.sleep(0.5)
    return False, "启动超时，请查看 logs\\bot.log"


def stop_gateway() -> tuple[bool, str]:
    import time

    pid = gateway_pid()
    if not pid:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return True, "网关未在运行"

    # 先温和地杀整棵进程树（venv 的 pythonw 是启动器，会再拉起解释器）
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    for _ in range(40):
        if not is_alive(pid):
            break
        time.sleep(0.25)

    # 兜底：仍然活着就自己 TerminateProcess
    if is_alive(pid):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
        for _ in range(20):
            if not is_alive(pid):
                break
            time.sleep(0.25)

    alive = is_alive(pid)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return (not alive), ("网关已停止" if not alive else "网关仍在运行，请手动结束进程")


def set_paused(paused: bool) -> None:
    if paused:
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.write_text("paused", encoding="utf-8")
    else:
        try:
            PAUSE_FLAG.unlink()
        except OSError:
            pass


def tail_log(lines: int = 200) -> str:
    path = PROJECT_ROOT / "logs" / "bot.log"
    if not path.is_file():
        return "(暂无日志)"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError as exc:
        return f"(读取日志失败: {exc})"
    return "".join(content[-lines:]).strip() or "(日志为空)"


def open_path(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606 - 仅用于打开本地文件