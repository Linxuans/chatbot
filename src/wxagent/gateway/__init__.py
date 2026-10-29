"""微信网关：唯一需要跑在 Windows、贴着微信客户端的一层。"""

from .base import BaseGateway, MessageHandler

__all__ = ["BaseGateway", "MessageHandler"]