# -*- coding: utf-8 -*-
"""环境自检：一条命令看清"为什么跑不起来"。

微信个人号方案最常见的三个坑：
1. 微信版本和注入 DLL 不匹配（差一个 build 就可能失效）；
2. .env 没配 / API Key 为空；
3. 10086 端口被上一次没退干净的进程占着。

doctor 把这些问题一次查完，并给出下一步动作。
"""
from __future__ import annotations

import os
import re
import socket
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT, Settings

ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}

WECHAT_KEYWORDS = ("微信", "wechat", "weixin")
# wcferry 的 spy.dll 里写死了它适配的微信版本
DLL_VERSION_RE = re.compile(rb"3\.9\.\d{1,2}\.\d{1,3}")

# 实测出来的对应关系（读各自 wheel 里的 spy.dll 得到，不是猜的）
WCFERRY_TO_WEIXIN = {
    "39.4.2.2": "3.9.12.17",
    "39.4.5.0": "3.9.12.17",
    "39.5.1.0": "3.9.12.51",
    "39.5.2.0": "3.9.12.51",
    "39.6.0.0": "3.9.12.56",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    advice: str = ""


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


DEFAULT_WECHAT_DIRS = (
    r"C:\Program Files\Tencent\WeChat",
    r"C:\Program Files (x86)\Tencent\WeChat",
    r"C:\Program Files\Tencent\Weixin",
    r"C:\Program Files (x86)\Tencent\Weixin",
)


def _file_version(path: Path) -> str:
    """读 PE 文件版本号，不依赖 pywin32。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - 非 Windows
        return ""

    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ""
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return ""

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", wintypes.DWORD),
            ("struct_version", wintypes.DWORD),
            ("file_version_ms", wintypes.DWORD),
            ("file_version_ls", wintypes.DWORD),
            ("product_version_ms", wintypes.DWORD),
            ("product_version_ls", wintypes.DWORD),
            ("flags_mask", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("file_os", wintypes.DWORD),
            ("file_type", wintypes.DWORD),
            ("file_subtype", wintypes.DWORD),
            ("date_ms", wintypes.DWORD),
            ("date_ls", wintypes.DWORD),
        ]

    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    ms, ls = info.file_version_ms, info.file_version_ls
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _registry_locations() -> list[Path]:
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []

    locations: list[Path] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key_path in roots:
        try:
            root = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        try:
                            display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        except OSError:
                            continue
                        lowered = display.lower()
                        if not any(k in lowered for k in ("微信", "weat", "weixin", "wechat")):
                            continue
                        try:
                            location = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip('" ')
                        except OSError:
                            continue
                        if location:
                            locations.append(Path(location))
                except OSError:
                    continue
    return locations


def _wechat_installs() -> list[dict[str, str]]:
    """找出真正可运行的微信客户端。

    注册表条目可能是卸载残留（目录还在、exe 没了），所以必须以能看到 exe 为准，
    版本号也读 exe 自身，而不是注册表里可能过时的 DisplayVersion。
    """
    candidates = _registry_locations() + [Path(p) for p in DEFAULT_WECHAT_DIRS]

    installs: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in candidates:
        for exe_name in ("WeChat.exe", "Weixin.exe"):
            exe = directory / exe_name
            if not exe.is_file():
                continue
            key = str(exe).lower()
            if key in seen:
                continue
            seen.add(key)
            installs.append(
                {
                    "path": str(exe),
                    "dir": str(directory),
                    "exe": exe_name,
                    "version": _file_version(exe),
                }
            )
    return installs


def _dll_target_version() -> str:
    try:
        import wcferry
    except ImportError:
        return ""
    dll = Path(wcferry.__file__).parent / "spy.dll"
    if not dll.is_file():
        return ""
    found = DLL_VERSION_RE.findall(dll.read_bytes())
    if not found:
        return ""
    versions = sorted({item.decode("ascii", "ignore") for item in found}, key=_version_tuple)
    return versions[-1]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _reachable(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_python() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        return Check("Python 版本", "fail", f"当前 {version}", "需要 Python 3.10 及以上")
    return Check("Python 版本", "ok", version)


def _check_platform() -> Check:
    if os.name != "nt":
        return Check(
            "运行平台", "fail", f"{sys.platform}",
            "微信网关只能在 Windows 上跑；本机只跑核心请用 --mode core",
        )
    return Check("运行平台", "ok", "Windows")


def _check_modules(need_gateway: bool) -> list[Check]:
    checks: list[Check] = []
    for module, hint in (("fastapi", "pip install -r requirements.txt"), ("httpx", "pip install -r requirements.txt"), ("dotenv", "pip install -r requirements.txt")):
        try:
            __import__(module)
            checks.append(Check(f"依赖 {module}", "ok"))
        except ImportError:
            checks.append(Check(f"依赖 {module}", "fail", "未安装", hint))

    if need_gateway:
        try:
            import wcferry  # noqa: F401
            checks.append(Check("依赖 wcferry", "ok"))
        except ImportError:
            checks.append(Check("依赖 wcferry", "fail", "未安装", "pip install -r requirements-gateway.txt"))
    return checks


def _check_env(cfg: Settings) -> Check:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return Check(".env 配置", "warn", "没有 .env，正在用默认值/环境变量", "Copy-Item .env.example .env 然后填 DEEPSEEK_API_KEY")
    return Check(".env 配置", "ok", str(env_path))


def _check_api_key(cfg: Settings) -> Check:
    if not cfg.api_key:
        return Check("模型 API Key", "fail", "为空", "在 .env 里填 DEEPSEEK_API_KEY")
    masked = cfg.api_key[:6] + "***" + cfg.api_key[-4:] if len(cfg.api_key) > 12 else "***"
    return Check("模型 API Key", "ok", masked)


def _check_bot_names(cfg: Settings) -> Check:
    if not cfg.bot_names:
        return Check("机器人昵称", "warn", "未配置，将自动读取登录账号昵称", "若 @ 识别不到，把微信昵称填进 BOT_NAMES")
    return Check("机器人昵称", "ok", " / ".join(cfg.bot_names))


def _check_wechat() -> list[Check]:
    installs = _wechat_installs()
    dll_version = _dll_target_version()

    if not installs:
        return [
            Check(
                "微信客户端", "fail",
                "没有找到可运行的微信（注册表可能有卸载残留，但目录里没有 exe）",
                "安装微信 3.9.12.17 专供机器人使用",
            )
        ]

    checks: list[Check] = []
    legacy = [i for i in installs if i["version"].startswith("3.")]
    modern = [i for i in installs if i["version"].startswith("4.")]

    checks.append(
        Check(
            "微信客户端", "ok",
            "；".join(f"{i['version'] or '版本未知'} {i['exe']}" for i in installs),
        )
    )

    if modern:
        checks.append(
            Check(
                "微信 4.x", "warn",
                "、".join(i["version"] for i in modern),
                "hook 只支持 3.9.x：4.x 无法注入。需另装 3.9.12.17 并用小号登录（可与 4.x 共存）",
            )
        )

    if not legacy:
        checks.append(
            Check(
                "可用客户端", "fail",
                "只有 4.x，没有 3.9.x",
                "装一个 3.9.12.17 客户端专供机器人；3.9 与 4.x 可同时安装，分别登录不同账号",
            )
        )
        return checks

    if not dll_version:
        checks.append(Check("注入 DLL 目标版本", "warn", "未能从 spy.dll 读出", "检查 wcferry 是否完整安装"))
        return checks

    best = max(legacy, key=lambda i: _version_tuple(i["version"]))
    installed = _version_tuple(best["version"])
    target = _version_tuple(dll_version)

    if installed == target:
        checks.append(Check("版本匹配", "ok", f"微信 {best['version']} = DLL 目标 {dll_version}"))
        return checks

    matched = [ver for ver, wx in WCFERRY_TO_WEIXIN.items() if _version_tuple(wx) == installed]
    options = []
    if matched:
        options.append(f"pip install wcferry=={max(matched, key=_version_tuple)}（适配你现在的微信）")
    options.append(f"或把微信换成 {dll_version}")

    status = "warn" if installed[:3] == target[:3] else "fail"
    checks.append(
        Check(
            "版本匹配", status,
            f"微信 {best['version']}，注入 DLL 按 {dll_version} 编译",
            "  ".join(options),
        )
    )
    return checks


def _check_port(cfg: Settings) -> Check:
    if _port_free(cfg.wcf_port):
        return Check("wcferry 端口", "ok", f"{cfg.wcf_port} 可用")
    return Check("wcferry 端口", "warn", f"{cfg.wcf_port} 被占用",
                 "可能是上次没退干净：关掉残留的 wcf.exe / python 进程，或改 WCF_PORT")


def _check_storage(cfg: Settings) -> Check:
    db = Path(cfg.db_path)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        probe = db.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("数据目录", "fail", str(db.parent), f"不可写：{exc}")
    return Check("数据目录", "ok", str(db.parent))


def _check_sqlite(cfg: Settings) -> Check:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.close()
    except sqlite3.Error as exc:
        return Check("SQLite", "fail", str(exc), "重装 Python（自带 sqlite3）")
    return Check("SQLite", "ok", sqlite3.sqlite_version)


def _check_api_call(cfg: Settings, llm=None) -> Check:
    """真发一次最小请求，确认 Key 有效、模型名可用。"""
    if not cfg.api_key:
        return Check("模型调用实测", "fail", "没有 API Key", "先在 .env 里填 DEEPSEEK_API_KEY")

    if llm is None:
        try:
            from .llm import LLM

            llm = LLM(cfg)
        except Exception as exc:  # pragma: no cover - 依赖缺失
            return Check("模型调用实测", "fail", f"初始化失败: {exc}", "检查 requirements.txt 是否装全")

    try:
        reply = llm.chat(
            [{"role": "user", "content": "只回复两个字：正常"}],
            temperature=0.0,
            max_tokens=16,
            timeout=30.0,
            retries=0,
        )
    except Exception as exc:
        return Check("模型调用实测", "fail", str(exc)[:160], "检查 Key 是否有效、余额是否充足、模型名是否正确")

    return Check("模型调用实测", "ok", f"{cfg.model} 回复：{reply.strip()[:40]}")


def _check_network() -> Check:
    if _reachable("api.deepseek.com"):
        return Check("模型接口连通", "ok", "api.deepseek.com:443")
    return Check("模型接口连通", "warn", "连不上 api.deepseek.com:443",
                 "检查网络/代理；公司网可能需要放行")


def run_doctor(cfg: Settings, need_gateway: bool = True) -> int:
    """打印体检结果，返回退出码：0=可以跑，1=有阻断项。"""
    checks: list[Check] = [_check_python()]
    if need_gateway:
        checks.insert(0, _check_platform())
    checks += _check_modules(need_gateway)
    checks.append(_check_env(cfg))
    checks.append(_check_api_key(cfg))
    checks.append(_check_bot_names(cfg))
    checks.append(_check_storage(cfg))
    checks.append(_check_sqlite(cfg))
    checks.append(_check_network())
    if cfg.check_api:
        checks.append(_check_api_call(cfg))
    if need_gateway:
        checks += _check_wechat()
        checks.append(_check_port(cfg))

    print()
    print("=== 环境自检 ===")
    for item in checks:
        print(f"{ICON.get(item.status, '[?]')} {item.name:<16} {item.detail}")
        if item.advice and item.status != "ok":
            print(f"       -> {item.advice}")

    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]

    print()
    if fails:
        print(f"结论：有 {len(fails)} 个阻断项，先按上面的 -> 修掉再启动。")
        return 1
    if warns:
        print(f"结论：可以启动，但有 {len(warns)} 个提醒项，建议先确认。")
        return 0
    print("结论：全部通过，可以直接启动。")
    return 0