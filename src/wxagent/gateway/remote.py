# -*- coding: utf-8 -*-
"""网关 -> 核心 的 HTTP 客户端（分离部署时用）。

网关只做"搬运"：把微信消息转成 JSON 丢给核心，再把核心返回的文本发回微信。
"""
from __future__ import annotations

import logging

import httpx

from ..agent import IncomingMessage, Reply

log = logging.getLogger(__name__)


class RemoteCore:
    def __init__(self, core_url: str, token: str = "", timeout: float = 120.0) -> None:
        self.core_url = core_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(
            base_url=self.core_url,
            timeout=httpx.Timeout(timeout, connect=8.0),
            headers={"X-Gateway-Token": token} if token else {},
            trust_env=False,
        )

    def health(self) -> dict:
        resp = self._client.get("/healthz")
        resp.raise_for_status()
        return resp.json()

    def announce_bot_names(self, names: list[str]) -> None:
        try:
            self._client.post("/api/bot-names", json=names)
        except httpx.HTTPError as exc:
            log.warning("同步机器人昵称失败: %s", exc)

    def __call__(self, msg: IncomingMessage) -> list[Reply]:
        payload = {
            "chat_id": msg.chat_id,
            "content": msg.content,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "chat_name": msg.chat_name,
            "msg_id": msg.msg_id,
            "ts": msg.ts,
            "is_group": msg.is_group,
            "is_at_me": msg.is_at_me,
            "is_self": msg.is_self,
            "kind": msg.kind,
            "quoted": msg.quoted,
        }
        try:
            resp = self._client.post("/api/ingest", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.error("调用核心失败: %s", exc)
            return []

        return [
            Reply(chat_id=msg.chat_id, text=item.get("text", ""), index=item.get("index", 0), total=item.get("total", 1))
            for item in data.get("replies", [])
        ]

    def close(self) -> None:
        self._client.close()