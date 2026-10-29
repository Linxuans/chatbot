# -*- coding: utf-8 -*-
"""会话记忆：滑动窗口 + 滚动摘要。

为什么不是简单"存全部历史"：
群聊消息量可能很大，全塞进 prompt 既贵又会被截断。
策略是最近 N 条原文（保证时效）+ 一段不断更新的摘要（保证长期记忆）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

SUMMARY_RULES = (
    "你是一个群聊记录压缩器。把给定的微信群聊压缩成一段背景摘要，供后续对话回忆使用。要求：\n"
    "1. 不超过 {limit} 字；\n"
    "2. 保留参与人昵称、讨论结论、待办事项、关键事实与数字；\n"
    "3. 丢掉寒暄、重复、无信息量的句子；\n"
    "4. 第三人称陈述，不要标题、不要编号、不要 Markdown；\n"
    "5. 只输出摘要正文。"
)


class SupportsChat(Protocol):
    def chat(self, messages: list[dict[str, str]], **kwargs) -> str: ...


def _clock(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "--:--"


class Memory:
    def __init__(self, store: Store, cfg: Settings) -> None:
        self.store = store
        self.cfg = cfg

    def record(
        self,
        chat_id: str,
        content: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        is_self: bool = False,
        ts: int | float = 0,
        kind: str = "text",
        msg_id: str = "",
    ) -> bool:
        return self.store.add_message(
            chat_id,
            content,
            sender_id=sender_id,
            sender_name=sender_name,
            is_self=is_self,
            ts=ts,
            kind=kind,
            msg_id=msg_id,
        )

    def transcript(self, chat_id: str) -> str:
        """拼出给模型的上下文：历史摘要 + 最近若干条原文。"""
        summary, _ = self.store.get_summary(chat_id)
        messages = self.store.recent(chat_id, self.cfg.context_messages)

        blocks: list[str] = []
        if summary:
            blocks.append(f"[更早的群聊摘要]\n{summary}")

        if messages:
            lines = []
            for item in messages:
                body = item.content if item.kind == "text" else f"[{item.kind}] {item.content}"
                lines.append(f"[{_clock(item.ts)}] {item.display_name}: {body}")
            blocks.append("[最近消息]\n" + "\n".join(lines))

        return "\n\n".join(blocks)

    def maybe_summarize(self, chat_id: str, llm: SupportsChat) -> bool:
        """新消息攒够 threshold 条就滚动压缩一次。失败不抛，只记日志。"""
        if self.cfg.summary_trigger <= 0:
            return False

        summary, last_rowid = self.store.get_summary(chat_id)
        pending = self.store.count_new(chat_id, last_rowid)
        if pending < self.cfg.summary_trigger:
            return False

        batch = self.store.after(chat_id, last_rowid, self.cfg.summary_trigger)
        if not batch:
            return False

        lines = []
        for item in batch:
            body = item.content if item.kind == "text" else f"[{item.kind}] {item.content}"
            lines.append(f"{item.display_name}: {body}")
        fresh = "\n".join(lines)

        user_prompt = f"[已有摘要]\n{summary or '（无）'}\n\n[新增群聊记录]\n{fresh}"
        try:
            merged = llm.chat(
                [
                    {"role": "system", "content": SUMMARY_RULES.format(limit=self.cfg.summary_max_chars)},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=800,
            )
        except Exception as exc:  # 摘要失败不能影响正文回复
            log.warning("摘要压缩失败 chat=%s: %s", chat_id, exc)
            return False

        merged = (merged or "").strip()
        if not merged:
            return False

        self.store.set_summary(chat_id, merged[: self.cfg.summary_max_chars * 2], batch[-1].rowid)
        log.info("已压缩群聊摘要 chat=%s 条数=%d", chat_id, len(batch))
        return True