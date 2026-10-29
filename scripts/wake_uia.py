# -*- coding: utf-8 -*-
"""热激活微信 4.x 的 Qt 无障碍 gate。

背景（实测 + 上游技术文档）：
微信 4.1.12+ 的聊天区是自绘渲染，对 UIA / MSAA 完全不暴露内容。
它内部 Qt 在每次无障碍查询前会看一个 QAccessible 的 active 字节，
冷启动为 0，于是 UIA 只能拿到 `Qt51514QWindowIcon` 空壳。
把这个字节在运行中的进程里写成 1，mmui:: 控件树会立刻物化。

本脚本只改运行中进程的 1 个字节：不注入 DLL、不挂钩子、不改磁盘文件。
重启微信后失效，需要重新执行（可做成启动脚本的一部分）。

    python scripts/wake_uia.py --status   # 只读，报告当前状态
    python scripts/wake_uia.py            # 激活并校验
"""
from __future__ import annotations

import ctypes
import re
import struct
import sys
import time
from ctypes import wintypes

import uiautomation as auto

# gate 检查的机器码形状：test rcx,rcx / jz / cmp byte ptr [rip+disp32],0 / jz
GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})\x00\x0f\x84", re.DOTALL
)
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PROCESS_ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008  # QUERY|VM_READ|VM_WRITE|VM_OP


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


def find_wechat():
    """找可见、标题像微信、且加载了 Weixin.dll 的主窗口。"""
    user32 = ctypes.windll.user32
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        # 标题必须就是「微信」，避免命中标题里恰好含「微信」的其它程序
        if title not in ("微信", "WeChat", "Weixin"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return True
        # 硬条件：该进程必须加载了 Weixin.dll
        if not module_base(pid.value):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        if area < 100000:
            return True
        results.append((area, hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(callback, 0)
    if not results:
        return None
    results.sort(reverse=True)
    _area, hwnd, pid, title = results[0]
    return hwnd, pid, title


def module_base(pid, wanted="weixin.dll"):
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for fn in (k32.Module32FirstW, k32.Module32NextW):
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        fn.restype = wintypes.BOOL
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if ctypes.cast(snap, ctypes.c_void_p).value == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == wanted:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return base, entry.szExePath
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return None


def pe_sections(data: bytes):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    count = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    base = coff + 20 + opt_size
    out = []
    for i in range(count):
        off = base + i * 40
        vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        chars = struct.unpack_from("<I", data, off + 36)[0]
        out.append({"rva": rva, "vsize": vsize, "raw": raw_size,
                    "ptr": raw_ptr, "chars": chars})
    return out


def rva_of(sections, offset: int):
    for s in sections:
        if s["ptr"] <= offset < s["ptr"] + s["raw"]:
            return s["rva"] + offset - s["ptr"]
    return None


def scan_gate_rva(dll_path: str) -> list[int]:
    data = open(dll_path, "rb").read()
    sections = pe_sections(data)
    if not sections:
        print("  Weixin.dll 不是可识别的 PE 文件")
        return []

    candidates = []
    for match in GATE_PATTERN.finditer(data):
        match_rva = rva_of(sections, match.start())
        disp_rva = rva_of(sections, match.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        section = next((s for s in sections if s["rva"] <= match_rva < s["rva"] + max(s["vsize"], s["raw"])), None)
        if not section or not (section["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        cmp_rva = disp_rva - 2
        target = cmp_rva + 7 + struct.unpack("<i", match.group("disp"))[0]
        tgt = next((s for s in sections if s["rva"] <= target < s["rva"] + max(s["vsize"], s["raw"])), None)
        if not tgt or not (tgt["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        candidates.append(target)
    return list(dict.fromkeys(candidates))


def read_byte(handle, address):
    k32 = ctypes.windll.kernel32
    buf = (ctypes.c_ubyte * 1)()
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(got))
    return int(buf[0]) if ok and got.value == 1 else None


def write_byte(handle, address, value):
    k32 = ctypes.windll.kernel32
    buf = (ctypes.c_ubyte * 1)(value & 0xFF)
    done = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(done))
    return bool(ok and done.value == 1)


def tree_ready(hwnd) -> tuple[str, bool]:
    ctrl = auto.ControlFromHandle(hwnd)
    if not ctrl:
        return "", False
    cls = ctrl.ClassName or ""
    kids = ctrl.GetChildren()[:8]
    if cls.startswith("mmui::") or any((k.ClassName or "").startswith("mmui::") for k in kids):
        return cls, True
    return cls, False


def main() -> int:
    status_only = "--status" in sys.argv

    found = find_wechat()
    if not found:
        print("没找到正在运行的微信主窗口（未登录 / 最小化到托盘都算）")
        return 2
    hwnd, pid, title = found
    print(f"微信窗口: hwnd={hwnd} pid={pid} title={title!r}")

    cls, ready = tree_ready(hwnd)
    print(f"当前控件树: 根节点={cls!r} mmui物化={ready}")
    if status_only:
        return 0 if ready else 1

    mod = module_base(pid)
    if not mod:
        print("没取到 Weixin.dll 基址")
        return 1
    base, dll_path = mod
    print(f"Weixin.dll: 基址=0x{base:X}  {dll_path}")

    rvas = scan_gate_rva(dll_path)
    if not rvas:
        print("没扫描到 gate 字节（该版本特征不匹配）")
        return 1
    print(f"候选 RVA: {', '.join('0x%X' % r for r in rvas[:6])}")

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(PROCESS_ACCESS, False, pid)
    if not handle:
        print("OpenProcess 失败（权限不足）：让脚本与微信保持相同权限级别，不要用管理员运行")
        return 1

    try:
        for rva in rvas:
            address = base + rva
            original = read_byte(handle, address)
            if original is None:
                print(f"  0x{rva:X} 读取失败，跳过")
                continue
            if original != 1:
                if not write_byte(handle, address, 1):
                    print(f"  0x{rva:X} 写入失败，跳过")
                    continue
                print(f"  0x{rva:X}: {original} -> 1")
            time.sleep(0.4)
            cls2, now = tree_ready(hwnd)
            if now:
                print(f"成功：控件树已物化，根节点={cls2!r}")
                return 0
            if original != 1:
                write_byte(handle, address, original)
            print(f"  0x{rva:X} 写入后树未物化，回滚并试下一个")
        print("所有候选都没生效：该版本 gate 位置未知，需要手工定位")
        return 1
    finally:
        k32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())