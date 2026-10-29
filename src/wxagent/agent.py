# -*- coding: utf-8 -*-
"""核心编排：一条群消息进来，决定要不要回、回什么、怎么切。

刻意不依赖任何微信 SDK：网关把消息喂进来，这里返回要发的文本列表。
这样同一套逻辑以后可以接 QQ 官方 Bot、企业微信、Telegram，核心不用改。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from .config import Settings
from .llm import LLM, LLMError
from .memory import Memory
from .pipeline import Cooldown, clean_text, detect_trigger, sanitize_reply, split_reply
from .prompt import build_system, build_user
from .store import Store
from .tools import TOOLS, run_tool

log = logging.getLogger(__name__)

# 一条回答最多分几条发，防止长文刷屏
MAX_CHUNKS = 4

HELP_TEXT = (
    "我在，直接 @我 问就行。\n"
    "可用命令（都是 @我 后接命令）：\n"
    "· /reset 清空本群的对话记忆\n"
    "· /status 看我的运行状态\n"
    "· /help 看这条说明"
)


@dataclass(slots=True)
class IncomingMessage:
    """网关 -> 核心的消息契约，跟微信解耦。"""

    chat_id: str
    content: str
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


@dataclass(slots=True)
class Reply:
    chat_id: str
    text: str
    index: int = 0
    total: int = 1


class AgentService:
    def __init__(self, cfg: Settings, *, store: Store | None = None, llm: LLM | None = None) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.memory = Memory(self.store, cfg)
        self.llm = llm or LLM(cfg)
        self.cooldown = Cooldown(cfg.cooldown_seconds, cfg.max_replies_per_minute)
        self.bot_name = cfg.bot_names[0] if cfg.bot_names else "小助手"

    # ---------------------------------------------------------------- 身份
    def set_bot_names(self, names: list[str]) -> None:
        """网关登录后把真实昵称告诉核心，@ 识别才准。"""
        clean = [n.strip() for n in names if n and n.strip()]
        if not clean:
            return
        merged = clean + [n for n in self.cfg.bot_names if n not in clean]
        self.cfg.bot_names = merged
        self.bot_name = merged[0]
        log.info("机器人昵称: %s", " / ".join(merged))

    # ---------------------------------------------------------------- 主入口
    def handle(self, msg: IncomingMessage) -> list[Reply]:
        if msg.is_self:
            return []

        content = clean_text(msg.content)
        if not content:
            return []

        if msg.msg_id and self.store.seen(msg.chat_id, msg.msg_id):
            log.debug("重复消息，忽略 msg_id=%s", msg.msg_id)
            return []

        if not self.cfg.room_allowed(msg.chat_id, msg.chat_name):
            log.debug("群不在白名单，忽略 chat=%s", msg.chat_name or msg.chat_id)
            return []

        should_reply, body = detect_trigger(
            content,
            mode=self.cfg.trigger_mode,
            prefix=self.cfg.trigger_prefix,
            bot_names=self.cfg.bot_names,
            is_group=msg.is_group,
            is_at_me=msg.is_at_me,
            private_ok=self.cfg.reply_in_private,
        )

        # 上下文要在写入本条之前取，避免同一句话出现两次
        context = self.memory.transcript(msg.chat_id) if should_reply else ""

        self.memory.record(
            msg.chat_id,
            content if msg.kind == "text" else f"[{msg.kind}] {content}",
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            is_self=False,
            ts=msg.ts or time.time(),
            kind="text" if msg.kind == "text" else msg.kind,
            msg_id=msg.msg_id,
        )

        if not should_reply:
            return []

        command_reply = self._handle_command(msg, body)
        if command_reply is not None:
            return self._build_replies(msg, command_reply, remember=False)

        allowed, reason = self.cooldown.check(msg.chat_id)
        if not allowed:
            log.info("触发限流(%s)，本条不回复 chat=%s", reason, msg.chat_id)
            return []

        try:
            raw = self._ask(
                [
                    {"role": "system", "content": build_system(self.cfg, self.bot_name)},
                    {
                        "role": "user",
                        "content": build_user(
                            room=msg.chat_name,
                            sender=msg.sender_name,
                            question=body,
                            context=context,
                            quoted=msg.quoted,
                        ),
                    },
                ]
            )
        except LLMError as exc:
            log.error("模型调用失败 chat=%s: %s", msg.chat_id, exc)
            if self.cfg.notify_on_error:
                return self._build_replies(msg, "（我的模型接口这会儿不通，稍后再 @ 我一次）")
            return []

        text = sanitize_reply(raw)
        if not text:
            log.warning("模型返回空内容 chat=%s", msg.chat_id)
            return []

        log.info("回复 chat=%s sender=%s chars=%d", msg.chat_name or msg.chat_id, msg.sender_name, len(text))
        replies = self._build_replies(msg, text)
        self._summarize_async(msg.chat_id)
        return replies

    # ---------------------------------------------------------------- 模型
    MAX_TOOL_ROUNDS = 2
    # 单条消息最多调几次工具 / 最多花多久，防止模型反复搜索把回复拖没
    MAX_TOOL_CALLS = 2
    MAX_TOOL_SECONDS = 20.0

    def _ask(self, messages: list[dict]) -> str:
        """调用模型；开了联网就让模型自己决定要不要先搜索。"""
        if not self.cfg.enable_web_search:
            return self.llm.chat(messages)

        used_tools = False
        tool_calls_used = 0
        started = time.time()
        message = self.llm.chat_message(messages, tools=TOOLS)
        for _ in range(self.MAX_TOOL_ROUNDS):
            calls = message.get("tool_calls") or []
            if not calls:
                break
            if tool_calls_used >= self.MAX_TOOL_CALLS or time.time() - started > self.MAX_TOOL_SECONDS:
                log.info("已达工具预算（%d 次 / %.0fs），直接收尾", tool_calls_used,
                         time.time() - started)
                used_tools = True
                break
            used_tools = True
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls[: max(1, self.MAX_TOOL_CALLS - tool_calls_used)]:
                tool_calls_used += 1
                function = call.get("function") or {}
                name = function.get("name") or ""
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                log.info("调用工具 %s %s", name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "content": run_tool(name, arguments),
                    }
                )
            message = self.llm.chat_message(messages, tools=TOOLS)

        text = (message.get("content") or "").strip()

        # 用过工具时再补一次「总结式回答」：
        # 否则模型可能停在思考中途（实测会吐出 "Let me try another query." 这种碎片），
        # 而且容易切成英文。
        if used_tools:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "请根据以上搜索结果，用中文直接回答我最初的问题。"
                        "不要再提搜索过程，不要再说要继续查，信息不足就直说不足。"
                    ),
                }
            )
            text = self.llm.chat(messages).strip()

        if not text:
            text = (message.get("reasoning_content") or "").strip()
        return text

    # ---------------------------------------------------------------- 命令
    def _handle_command(self, msg: IncomingMessage, body: str) -> str | None:
        text = body.strip()
        if not text.startswith("/"):
            return None

        parts = text[1:].split()
        cmd = parts[0].lower() if parts else ""
        if not cmd:
            return None

        if cmd in {"help", "帮助", "?"}:
            return HELP_TEXT

        if cmd in {"status", "状态"}:
            stats = self.store.stats()
            return (
                f"模型 {self.cfg.model}｜已记 {stats['chats']} 个会话 / {stats['messages']} 条消息｜"
                f"触发方式 {self.cfg.trigger_mode}｜冷却 {self.cfg.cooldown_seconds:g}s"
            )

        if cmd in {"reset", "清空", "clear"}:
            if not self.cfg.is_admin(msg.sender_id):
                return "只有管理员能清空记忆。"
            self.store.reset_chat(msg.chat_id)
            log.info("已清空记忆 chat=%s by=%s", msg.chat_id, msg.sender_name)
            return "已清空本群的对话记忆。"

        return None  # 不认识的命令 -> 当普通问题

    # ---------------------------------------------------------------- 组装
    def _build_replies(self, msg: IncomingMessage, text: str, *, remember: bool = True) -> list[Reply]:
        chunks = split_reply(text, self.cfg.max_reply_chars)[:MAX_CHUNKS]
        if not chunks:
            return []

        replies: list[Reply] = []
        for index, chunk in enumerate(chunks):
            if remember:
                # 自己的回复也入库，后续追问才有连贯上下文
                self.memory.record(
                    msg.chat_id,
                    chunk,
                    sender_name=self.bot_name,
                    is_self=True,
                    ts=time.time(),
                    kind="text",
                )
            replies.append(Reply(chat_id=msg.chat_id, text=chunk, index=index, total=len(chunks)))
        return replies

    def _summarize_async(self, chat_id: str) -> None:
        if self.cfg.summary_trigger <= 0:
            return

        def run() -> None:
            try:
                self.memory.maybe_summarize(chat_id, self.llm)
            except Exception:  # pragma: no cover - 兜底
                log.exception("后台摘要任务异常 chat=%s", chat_id)

        threading.Thread(target=run, name="wxagent-summarize", daemon=True).start()