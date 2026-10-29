# -*- coding: utf-8 -*-
"""统一启动入口。

常用：
    python run.py                    # 按 .env 里的 MODE 启动
    python run.py --mode embedded    # 单机：微信网关 + 智能体核心同进程（最快跑通）
    python run.py --mode gateway     # 只跑微信网关，POST 到远端的核心
    python run.py --mode core        # 只跑核心 HTTP 服务（Docker 里跑这个）
    python run.py --mode probe       # 只打印群消息和 @ 判定，不调模型（校准用）
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wxagent.agent import AgentService, IncomingMessage  # noqa: E402
from wxagent.config import Settings, setup_logging  # noqa: E402

log = logging.getLogger("wxagent.run")


def _warn(cfg: Settings) -> None:
    for issue in cfg.problems():
        log.warning("配置提醒: %s", issue)


def _acquire_single_instance(name: str = "wxagent-uia-gateway"):
    """同一时间只允许一个微信网关实例，否则两个进程会互相点击同一个会话。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def _build_service(cfg: Settings) -> AgentService:
    return AgentService(cfg)


def _load_gateway_class():
    """延迟导入：核心模式在 Linux/Docker 下不需要 wcferry。"""
    try:
        from wxagent.gateway.wcferry_gw import WcferryGateway
    except ImportError as exc:
        raise SystemExit(
            "微信网关依赖 wcferry，只能在装有微信的 Windows 上运行。\n"
            f"原始错误: {exc}\n"
            "安装: pip install -r requirements-gateway.txt"
        ) from exc
    return WcferryGateway


def run_embedded(cfg: Settings) -> None:
    gateway_cls = _load_gateway_class()
    service = _build_service(cfg)
    gateway = gateway_cls(service.handle, port=cfg.wcf_port, debug=cfg.wcf_debug)
    service.set_bot_names(gateway.self_names())
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出…")
        gateway.stop()


def run_gateway(cfg: Settings) -> None:
    gateway_cls = _load_gateway_class()
    from wxagent.gateway.remote import RemoteCore

    core = RemoteCore(cfg.core_url, cfg.gateway_token)
    try:
        health = core.health()
        log.info("已连接核心 %s，模型 %s", cfg.core_url, health.get("model"))
    except Exception as exc:
        raise SystemExit(f"连不上核心 {cfg.core_url}: {exc}") from exc

    gateway = gateway_cls(core, port=cfg.wcf_port, debug=cfg.wcf_debug)
    core.announce_bot_names(gateway.self_names())
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出…")
        gateway.stop()


def run_core(cfg: Settings) -> None:
    import uvicorn

    from wxagent.core_api import create_app

    service = _build_service(cfg)
    app = create_app(service, cfg.gateway_token)
    log.info("核心服务启动 http://%s:%s（模型 %s）", cfg.core_host, cfg.core_port, cfg.model)
    uvicorn.run(app, host=cfg.core_host, port=cfg.core_port, log_level="warning")


def run_uia(cfg: Settings) -> None:
    """微信 4.x 界面自动化模式（不注入，依赖无障碍控件树）。"""
    from wxagent.gateway.uia_gw import UiaGateway

    lock = _acquire_single_instance()
    if lock is None:
        raise SystemExit("已经有一个微信网关在运行了。先关掉它（或用任务管理器结束 python 进程）再启动。")

    service = _build_service(cfg)
    gateway = UiaGateway(
        service.handle,
        bot_names=cfg.bot_names or [service.bot_name],
        poll_interval=cfg.uia_poll_interval,
        allowed_chats=cfg.allowed_rooms,
    )
    gateway.start()
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出…")
    finally:
        gateway.stop()


def run_check(cfg: Settings) -> None:
    """环境自检：版本、依赖、端口、Key 一次查完。"""
    from wxagent.doctor import run_doctor

    raise SystemExit(run_doctor(cfg, need_gateway=cfg.mode != "core"))


def run_probe(cfg: Settings) -> None:
    """校准模式：把收到的群消息原样打出来，看清 @ 识别到底命中了没有。"""
    gateway_cls = _load_gateway_class()

    def handler(msg: IncomingMessage):
        print("-" * 70)
        print(f"群      : {msg.chat_name or '(私聊)'}  id={msg.chat_id}")
        print(f"发送者  : {msg.sender_name}  wxid={msg.sender_id}")
        print(f"类型    : {msg.kind}   @我: {msg.is_at_me}   自己: {msg.is_self}")
        print(f"内容    : {msg.content!r}")
        return []

    gateway = gateway_cls(handler, port=cfg.wcf_port, debug=cfg.wcf_debug)
    print(f"机器人昵称: {gateway.self_names()}")
    print(f"机器人 wxid: {gateway.self_wxid()}")
    print("现在到群里 @ 一下机器人，观察上面的输出。Ctrl+C 退出。")
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        gateway.stop()


MODES = {
    "doctor": run_check,
    "uia": run_uia,
    "embedded": run_embedded,
    "gateway": run_gateway,
    "core": run_core,
    "probe": run_probe,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微信群聊智能体")
    parser.add_argument("--mode", choices=sorted(MODES), help="运行模式，默认取 .env 的 MODE")
    parser.add_argument("--env", help="指定 .env 文件路径")
    parser.add_argument("--core-url", help="gateway 模式下核心地址，覆盖 CORE_URL")
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="搭配 --mode doctor：真发一次模型请求，验证 Key 与模型名",
    )
    args = parser.parse_args(argv)

    cfg = Settings.load(args.env)
    cfg.check_api = bool(args.check_api)
    if args.core_url:
        cfg.core_url = args.core_url.rstrip("/")
    setup_logging(cfg.log_level, cfg.log_file)
    _warn(cfg)

    mode = args.mode or cfg.mode
    if mode not in MODES:
        raise SystemExit(f"未知模式 {mode}，可选: {', '.join(sorted(MODES))}")

    if mode == "doctor":
        run_check(cfg)
        return 0

    log.info("启动模式: %s", mode)
    MODES[mode](cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())