# -*- coding: utf-8 -*-
"""配置加载：全部走环境变量 / .env，方便 Docker 覆盖。

原则：代码里不出现任何密钥或与平台绑定的硬编码，
换模型、换群、换触发方式都只改 .env。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # 核心镜像里装了，这里只是容错
        return
    env_file = os.getenv("ENV_FILE") or str(PROJECT_ROOT / ".env")
    if Path(env_file).is_file():
        load_dotenv(env_file, override=False, encoding="utf-8")


def _s(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    raw = _s(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "是"}


def _list(name: str) -> list[str]:
    raw = _s(name)
    for sep in ("，", "；", ";", "\n"):
        raw = raw.replace(sep, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    # ---- 模型 ----
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 1200
    timeout: float = 90.0

    # ---- 机器人身份 ----
    bot_names: list[str] = field(default_factory=list)
    extra_system_prompt: str = ""

    # ---- 触发规则 ----
    trigger_mode: str = "at"
    trigger_prefix: str = "/"
    reply_in_private: bool = True
    allowed_rooms: list[str] = field(default_factory=list)
    blocked_rooms: list[str] = field(default_factory=list)
    admin_wxids: list[str] = field(default_factory=list)

    # ---- 上下文与记忆 ----
    context_messages: int = 12
    summary_trigger: int = 40
    summary_keep: int = 12
    summary_max_chars: int = 400

    # ---- 防刷 ----
    cooldown_seconds: float = 3.0
    max_replies_per_minute: int = 6
    max_reply_chars: int = 1800
    notify_on_error: bool = False

    # ---- 存储与日志 ----
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "bot.db")
    log_level: str = "INFO"
    # 仅由 --check-api 命令行开关设置，不进 .env
    check_api: bool = False
    log_file: str = ""

    # ---- 部署 ----
    mode: str = "embedded"
    core_url: str = "http://127.0.0.1:8000"
    core_host: str = "0.0.0.0"
    core_port: int = 8000
    gateway_token: str = ""
    wcf_port: int = 10086
    wcf_debug: bool = False

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Settings":
        if env_file:
            os.environ.setdefault("ENV_FILE", str(env_file))
        _load_dotenv()
        return cls(
            api_key=_s("DEEPSEEK_API_KEY"),
            base_url=_s("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=_s("DEEPSEEK_MODEL", "deepseek-chat"),
            temperature=_f("TEMPERATURE", 0.7),
            max_tokens=_i("MAX_TOKENS", 1200),
            timeout=_f("REQUEST_TIMEOUT", 90.0),
            bot_names=_list("BOT_NAMES"),
            extra_system_prompt=_s("EXTRA_SYSTEM_PROMPT"),
            trigger_mode=_s("TRIGGER_MODE", "at").lower(),
            trigger_prefix=_s("TRIGGER_PREFIX", "/"),
            reply_in_private=_b("REPLY_IN_PRIVATE", True),
            allowed_rooms=_list("ALLOWED_ROOMS"),
            blocked_rooms=_list("BLOCKED_ROOMS"),
            admin_wxids=_list("ADMIN_WXIDS"),
            context_messages=_i("CONTEXT_MESSAGES", 12),
            summary_trigger=_i("SUMMARY_TRIGGER", 40),
            summary_keep=_i("SUMMARY_KEEP", 12),
            summary_max_chars=_i("SUMMARY_MAX_CHARS", 400),
            cooldown_seconds=_f("COOLDOWN_SECONDS", 3.0),
            max_replies_per_minute=_i("MAX_REPLIES_PER_MINUTE", 6),
            max_reply_chars=_i("MAX_REPLY_CHARS", 1800),
            notify_on_error=_b("NOTIFY_ON_ERROR", False),
            db_path=Path(_s("DB_PATH") or (PROJECT_ROOT / "data" / "bot.db")),
            log_level=_s("LOG_LEVEL", "INFO").upper(),
            log_file=_s("LOG_FILE"),
            mode=_s("MODE", "embedded").lower(),
            core_url=_s("CORE_URL", "http://127.0.0.1:8000").rstrip("/"),
            core_host=_s("CORE_HOST", "0.0.0.0"),
            core_port=_i("CORE_PORT", 8000),
            gateway_token=_s("GATEWAY_TOKEN"),
            wcf_port=_i("WCF_PORT", 10086),
            wcf_debug=_b("WCF_DEBUG", False),
        )

    # ---- 辅助判定 ----
    def room_allowed(self, chat_id: str, chat_name: str = "") -> bool:
        keys = {k for k in (chat_id, chat_name) if k}
        if keys & set(self.blocked_rooms):
            return False
        if not self.allowed_rooms:
            return True
        return bool(keys & set(self.allowed_rooms))

    def is_admin(self, wxid: str) -> bool:
        return not self.admin_wxids or wxid in self.admin_wxids

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.api_key:
            issues.append("DEEPSEEK_API_KEY 未配置，模型调用会失败")
        if self.trigger_mode not in {"at", "prefix", "all"}:
            issues.append(f"TRIGGER_MODE={self.trigger_mode} 非法，应为 at / prefix / all")
        if self.context_messages < 1:
            issues.append("CONTEXT_MESSAGES 至少为 1")
        if self.cooldown_seconds < 0:
            issues.append("COOLDOWN_SECONDS 不能为负")
        return issues


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """统一日志：控制台 + 可选文件，Windows 下强制 UTF-8 避免中文乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)