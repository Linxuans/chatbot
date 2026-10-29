# -*- coding: utf-8 -*-
"""把核心暴露成 HTTP 服务：网关和核心可以分机器部署（Docker 那侧跑这个）。

为什么拆开：微信客户端只能在 Windows 上跑，而容器更适合跑无状态服务。
网关 POST 一条消息，核心回一组要发送的文本。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentService, IncomingMessage
from . import __version__

log = logging.getLogger(__name__)


class IngestRequest(BaseModel):
    chat_id: str = Field(..., description="群 roomid 或好友 wxid")
    content: str = ""
    sender_id: str = ""
    sender_name: str = ""
    chat_name: str = ""
    msg_id: str = ""
    ts: int = 0
    is_group: bool = True
    is_at_me: bool = False
    is_self: bool = False
    kind: str = "text"
    quoted: str = ""


class ReplyOut(BaseModel):
    text: str
    index: int = 0
    total: int = 1


class IngestResponse(BaseModel):
    replies: list[ReplyOut] = Field(default_factory=list)


class ResetRequest(BaseModel):
    chat_id: str


class OkResponse(BaseModel):
    ok: bool = True
    detail: str = ""


def create_app(service: AgentService, gateway_token: str = "") -> FastAPI:
    app = FastAPI(title="wx-agent-core", version=__version__)

    def guard(token: str | None) -> None:
        if gateway_token and token != gateway_token:
            raise HTTPException(status_code=401, detail="gateway token 不匹配")

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "model": service.cfg.model,
            "trigger_mode": service.cfg.trigger_mode,
            "stats": service.store.stats(),
        }

    @app.post("/api/ingest", response_model=IngestResponse)
    def ingest(
        req: IngestRequest,
        x_gateway_token: str | None = Header(default=None, alias="X-Gateway-Token"),
    ) -> IngestResponse:
        guard(x_gateway_token)
        msg = IncomingMessage(
            chat_id=req.chat_id,
            content=req.content,
            sender_id=req.sender_id,
            sender_name=req.sender_name,
            chat_name=req.chat_name,
            msg_id=req.msg_id,
            ts=req.ts,
            is_group=req.is_group,
            is_at_me=req.is_at_me,
            is_self=req.is_self,
            kind=req.kind,
            quoted=req.quoted,
        )
        replies = service.handle(msg)
        return IngestResponse(
            replies=[ReplyOut(text=r.text, index=r.index, total=r.total) for r in replies]
        )

    @app.post("/api/reset", response_model=OkResponse)
    def reset(
        req: ResetRequest,
        x_gateway_token: str | None = Header(default=None, alias="X-Gateway-Token"),
    ) -> OkResponse:
        guard(x_gateway_token)
        service.store.reset_chat(req.chat_id)
        return OkResponse(detail=f"已清空 {req.chat_id}")

    @app.post("/api/bot-names", response_model=OkResponse)
    def bot_names(
        names: list[str],
        x_gateway_token: str | None = Header(default=None, alias="X-Gateway-Token"),
    ) -> OkResponse:
        guard(x_gateway_token)
        service.set_bot_names(names)
        return OkResponse(detail=", ".join(service.cfg.bot_names))

    return app