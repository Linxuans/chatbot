# -*- coding: utf-8 -*-
"""微信 4.x 的 Qt 无障碍 gate 控制。

背景：微信 4.1.12+ 聊天区自绘渲染，对 UIA / MSAA 不暴露内容。
进程内有一个 QAccessible active 字节，冷启动为 0，且会被周期性重置（实测约 5~6 秒）。
把它写成 1，mmui:: 控件树立刻物化；用后台线程每 2 秒补写即可持续保持。

只改运行中进程的 1 个字节：不注入 DLL、不挂钩子、不改磁盘文件，微信重启即恢复。
"""
from __future__ import annotations

import ctypes
import logging
import re
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})\x00\x0f\x84", re.DOTALL
)
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PROCESS_ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008
WINDOW_TITLE_EXACT = ("微信", "WeChat", "Weixin")
SW_RESTORE = 9
SW_SHOW = 5
# 主界面至少这么大；登录窗约 350x475，会被挡掉
MIN_MAIN_WINDOW_AREA = 260_000

# 扫描结果按 DLL 身份缓存：198MB 扫一次约 8 秒，不能每次重连都扫
_RVA_CACHE: dict[str, int] = {}


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


def module_base(pid: int, wanted: str = "weixin.dll") -> Optional[tuple[int, str]]:
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for fn in (k32.Module32FirstW, k32.Module32NextW):
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        fn.restype = wintypes.BOOL

    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if ctypes.cast(snapshot, ctypes.c_void_p).value == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == wanted:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return int(base), entry.szExePath
            ok = k32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return None


def is_process_alive(pid: int) -> bool:
    if not pid:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def find_wechat_window(require_module: bool = True) -> Optional[tuple[int, int, str]]:
    """返回 (hwnd, pid, title)：可见或最小化的微信主窗口。"""
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = (buf.value or "").strip()
        if title not in WINDOW_TITLE_EXACT:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return True
        if require_module and not module_base(pid.value):
            return True
        minimised = bool(user32.IsIconic(hwnd))
        visible = bool(user32.IsWindowVisible(hwnd))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        class_name = cls.value or ""
        # 登录窗也叫「微信」，但不是主界面，绑定它没有意义
        if "Login" in class_name:
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        # 最小化/隐藏时 GetWindowRect 会退化成小矩形，不能用面积过滤——
        # 否则窗口一旦最小化就再也找不到、也就无法还原
        if not minimised and visible and area < MIN_MAIN_WINDOW_AREA:
            return True
        log.debug("候选窗口 hwnd=%s pid=%s 可见=%s 最小化=%s", hwnd, pid.value, visible, minimised)
        found.append((area, hwnd, pid.value, title))
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return None
    found.sort(reverse=True)
    _area, hwnd, pid, title = found[0]
    return hwnd, pid, title


def window_state(hwnd: int) -> tuple[bool, bool]:
    """(是否可见, 是否最小化)"""
    user32 = ctypes.windll.user32
    return bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd))


def restore_window(hwnd: int, foreground: bool = False) -> None:
    """把最小化/隐藏的窗口还原；foreground=True 时才抢焦点。"""
    user32 = ctypes.windll.user32
    visible, minimised = window_state(hwnd)
    if not visible:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
    if minimised or not visible:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        log.info("已还原微信窗口（最小化/隐藏状态）")
    if foreground:
        try:
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
        except Exception:
            pass


def pe_sections(data: bytes):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    count = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    start = coff + 20 + opt_size
    sections = []
    for i in range(count):
        off = start + i * 40
        vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        chars = struct.unpack_from("<I", data, off + 36)[0]
        sections.append({"rva": rva, "vsize": vsize, "raw": raw_size,
                         "ptr": raw_ptr, "chars": chars})
    return sections


def _rva_of(sections, offset: int):
    for section in sections:
        if section["ptr"] <= offset < section["ptr"] + section["raw"]:
            return section["rva"] + offset - section["ptr"]
    return None


def scan_gate_rva(dll_path: str) -> list[int]:
    """在 Weixin.dll 里按机器码特征定位 gate 字节的 RVA。"""
    with open(dll_path, "rb") as handle:
        data = handle.read()
    sections = pe_sections(data)
    if not sections:
        return []

    candidates: list[int] = []
    for match in GATE_PATTERN.finditer(data):
        match_rva = _rva_of(sections, match.start())
        disp_rva = _rva_of(sections, match.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        section = next(
            (s for s in sections
             if s["rva"] <= match_rva < s["rva"] + max(s["vsize"], s["raw"])),
            None,
        )
        if not section or not (section["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        compare_rva = disp_rva - 2
        target = compare_rva + 7 + struct.unpack("<i", match.group("disp"))[0]
        target_section = next(
            (s for s in sections
             if s["rva"] <= target < s["rva"] + max(s["vsize"], s["raw"])),
            None,
        )
        if not target_section or not (target_section["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        candidates.append(target)
    return list(dict.fromkeys(candidates))


def gate_rva_cached(dll_path: str) -> int:
    """带缓存的 gate 定位：同一份 Weixin.dll 只扫一次。"""
    path = Path(dll_path)
    try:
        stat = path.stat()
        identity = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        return 0

    if identity in _RVA_CACHE:
        return _RVA_CACHE[identity]

    rvas = scan_gate_rva(dll_path)
    if not rvas:
        return 0
    _RVA_CACHE[identity] = rvas[0]
    log.info("扫描出 gate RVA: 0x%X（已缓存）", rvas[0])
    return rvas[0]


class AccessibilityGate:
    """保持微信的 mmui:: 控件树处于物化状态，并能在微信重启后自动恢复。"""

    def __init__(self, refresh_seconds: float = 2.0) -> None:
        self.refresh_seconds = refresh_seconds
        self.hwnd: int = 0
        self.pid: int = 0
        self._address: int = 0
        self._handle = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_missing_log = 0.0

    # ---------------------------------------------------------------- 底层
    def _read(self, address: int):
        k32 = ctypes.windll.kernel32
        buf = (ctypes.c_ubyte * 1)()
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self._handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(got))
        return int(buf[0]) if ok and got.value == 1 else None

    def _write(self, address: int, value: int) -> bool:
        k32 = ctypes.windll.kernel32
        buf = (ctypes.c_ubyte * 1)(value & 0xFF)
        done = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(self._handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(done))
        return bool(ok and done.value == 1)

    def _close_handle(self) -> None:
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    # ---------------------------------------------------------------- 控制
    def attach(self) -> bool:
        """找到微信并算出 gate 地址。已 attach 且进程没变时直接复用。"""
        with self._lock:
            found = find_wechat_window()
            if not found:
                now = time.time()
                if now - self._last_missing_log > 60:
                    log.warning("没找到正在运行的微信主窗口（等待微信启动/恢复登录）")
                    self._last_missing_log = now
                return False
            hwnd, pid, _title = found

            if pid == self.pid and self._handle and is_process_alive(pid):
                self.hwnd = hwnd
                return True

            self._close_handle()
            module = module_base(pid)
            if not module:
                log.error("进程 %s 没有加载 Weixin.dll", pid)
                return False
            base, dll_path = module

            rva = gate_rva_cached(dll_path)
            if not rva:
                log.error("没能从 %s 定位 gate 字节", dll_path)
                return False

            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            handle = k32.OpenProcess(PROCESS_ACCESS, False, pid)
            if not handle:
                log.error("OpenProcess 失败：请让本程序与微信保持相同权限级别")
                return False

            self.hwnd = hwnd
            self.pid = pid
            self._handle = handle
            self._address = base + rva
            log.info("已 attach 微信: pid=%s base=0x%X rva=0x%X", pid, base, rva)
            return True

    def open_gate(self) -> bool:
        if not self._handle:
            return False
        if self._read(self._address) == 1:
            return True
        return self._write(self._address, 1)

    def ensure(self) -> bool:
        """保证 gate 处于打开状态；微信重启后会自动重新 attach。"""
        if not self.pid or not is_process_alive(self.pid) or not self.attach():
            if not self.attach():
                return False
        return self.open_gate()

    def start(self) -> bool:
        """启动保活线程。即使此刻微信不可用也不返回失败——线程会持续重试。"""
        ready = False
        if self.attach() and self.open_gate():
            ready = True
        else:
            log.warning("微信当前不可用（未启动 / 未登录），后台等待…")

        def keeper() -> None:
            while not self._stop.is_set():
                try:
                    if not self.ensure():
                        log.debug("gate 保活：微信暂不可用")
                except Exception:
                    log.debug("gate 保活异常", exc_info=True)
                self._stop.wait(self.refresh_seconds)

        self._thread = threading.Thread(target=keeper, name="uia-gate", daemon=True)
        self._thread.start()
        return ready

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close_handle()