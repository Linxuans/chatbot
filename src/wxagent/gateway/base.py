# -*- coding: utf-8 -*-
"""网关抽象：把"收到消息"和"发出消息"收敛成两个动作。

换成 QQ 官方 Bot / 企业微信时，只要再实现一个 BaseGateway 子类，
核心的智能体逻辑一行都不用动。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable

from ..agent import IncomingMessage, Reply

log = logging.getLogger(__name__)

# 网关收到消息 -> 返回要发的回复（空列表表示不回）
MessageHandler = Callable[[IncomingMessage], list[Reply]]


class BaseGateway(ABC):
    def __init__(self, handler: MessageHandler) -> None:
        self.handler = handler

    @abstractmethod
    def run_forever(self) -> None:
        """阻塞运行，直到收到停止信号。"""

    @abstractmethod
    def send_text(self, chat_id: str, text: str) -> bool:
        """发一条文本到群或私聊。"""

    def stop(self) -> None:  # pragma: no cover - 子类按需覆盖
        pass

    def dispatch(self, msg: IncomingMessage) -> list[Reply]:
        """统一的异常边界：单条消息处理失败不能把网关打挂。"""
        try:
            return self.handler(msg) or []
        except Exception:
            log.exception("处理消息失败 chat=%s", msg.chat_id)
            return []

    def deliver(self, chat_id: str, replies: list[Reply]) -> None:
        for reply in replies:
            if not reply.text.strip():
                continue
            ok = self.send_text(chat_id, reply.text)
            log.info(
                "已发送 chat=%s 第%d/%d条 %s",
                chat_id, reply.index + 1, reply.total, "成功" if ok else "失败",
            )