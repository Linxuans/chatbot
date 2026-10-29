# -*- coding: utf-8 -*-
"""编排层单测：用假 LLM 驱动，验证"该不该回、回什么、记什么"。"""
from __future__ import annotations

import pytest

from wxagent.agent import AgentService, IncomingMessage
from wxagent.config import Settings
from wxagent.llm import LLMError
from wxagent.memory import Memory
from wxagent.store import Store


class FakeLLM:
    def __init__(self, reply: str = "这是回答。") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []
        self.fail: Exception | None = None

    def chat(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        if self.fail:
            raise self.fail
        return self.reply


def build(tmp_path, **overrides) -> AgentService:
    params = dict(
        api_key="test-key",
        db_path=tmp_path / "bot.db",
        bot_names=["小助手"],
        cooldown_seconds=0.0,
        max_replies_per_minute=0,
        context_messages=5,
        summary_trigger=0,
    )
    params.update(overrides)
    cfg = Settings(**params)
    return AgentService(cfg, llm=FakeLLM())


def test_at_message_gets_reply(tmp_path):
    service = build(tmp_path)
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 你好", sender_name="张三", is_at_me=True, msg_id="m1")
    )
    assert len(replies) == 1
    assert replies[0].text == "这是回答。"
    assert len(service.llm.calls) == 1


def test_plain_message_recorded_but_silent(tmp_path):
    service = build(tmp_path)
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="今天中午吃什么", sender_name="李四", msg_id="m2")
    )
    assert replies == []
    assert service.llm.calls == []
    assert service.store.stats()["messages"] == 1


def test_duplicate_message_ignored(tmp_path):
    service = build(tmp_path)
    msg = IncomingMessage(chat_id="room1", content="@小助手 你好", sender_name="张三", is_at_me=True, msg_id="dup")
    service.handle(msg)
    service.handle(msg)
    assert len(service.llm.calls) == 1


def test_context_includes_previous_messages(tmp_path):
    service = build(tmp_path)
    service.handle(IncomingMessage(chat_id="room1", content="方案定在周三了", sender_name="李四", msg_id="a1"))
    service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 方案是哪天", sender_name="张三", is_at_me=True, msg_id="a2")
    )
    user_prompt = service.llm.calls[0][1]["content"]
    assert "方案定在周三了" in user_prompt
    assert "李四" in user_prompt


def test_second_chat_is_isolated(tmp_path):
    service = build(tmp_path)
    service.handle(IncomingMessage(chat_id="room1", content="群一的秘密", sender_name="A", msg_id="b1"))
    service.handle(
        IncomingMessage(chat_id="room2", content="@小助手 说点啥", sender_name="B", is_at_me=True, msg_id="b2")
    )
    user_prompt = service.llm.calls[0][1]["content"]
    assert "群一的秘密" not in user_prompt


def test_help_command(tmp_path):
    service = build(tmp_path)
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 /help", sender_name="张三", is_at_me=True, msg_id="c1")
    )
    assert len(replies) == 1
    assert "/reset" in replies[0].text
    assert service.llm.calls == []


def test_reset_command_clears_memory(tmp_path):
    service = build(tmp_path)
    service.handle(IncomingMessage(chat_id="room1", content="随便聊聊", sender_name="A", msg_id="d1"))
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 /reset", sender_name="A", is_at_me=True, msg_id="d2")
    )
    assert "已清空" in replies[0].text
    assert service.store.stats()["messages"] == 0


def test_markdown_is_stripped(tmp_path):
    service = build(tmp_path)
    service.llm.reply = "**明天**要下雨，记得 `带伞`"
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 明天天气", sender_name="A", is_at_me=True, msg_id="e1")
    )
    assert "**" not in replies[0].text
    assert "`" not in replies[0].text


def test_long_reply_is_split(tmp_path):
    service = build(tmp_path, max_reply_chars=60)
    service.llm.reply = "第一句话在这里。" * 10
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 详细说说", sender_name="A", is_at_me=True, msg_id="f1")
    )
    assert len(replies) > 1
    assert all(len(r.text) <= 60 for r in replies)
    assert [r.index for r in replies] == list(range(len(replies)))


def test_private_message_answered(tmp_path):
    service = build(tmp_path)
    replies = service.handle(
        IncomingMessage(chat_id="wxid_abc", content="在吗", sender_name="张三", is_group=False, msg_id="g1")
    )
    assert len(replies) == 1


def test_room_whitelist(tmp_path):
    service = build(tmp_path, allowed_rooms=["只允许这个群"])
    replies = service.handle(
        IncomingMessage(chat_id="room1", chat_name="别的群", content="@小助手 你好", sender_name="A", is_at_me=True, msg_id="h1")
    )
    assert replies == []


def test_blocked_room(tmp_path):
    service = build(tmp_path, blocked_rooms=["静音的群"])
    replies = service.handle(
        IncomingMessage(chat_id="room1", chat_name="静音的群", content="@小助手 你好", sender_name="A", is_at_me=True, msg_id="i1")
    )
    assert replies == []


def test_llm_error_does_not_crash(tmp_path):
    service = build(tmp_path)
    service.llm.fail = LLMError("boom")
    replies = service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 你好", sender_name="A", is_at_me=True, msg_id="j1")
    )
    assert replies == []


def test_bot_reply_is_recorded_for_follow_up(tmp_path):
    service = build(tmp_path)
    service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 你好", sender_name="A", is_at_me=True, msg_id="k1")
    )
    service.handle(
        IncomingMessage(chat_id="room1", content="@小助手 继续", sender_name="A", is_at_me=True, msg_id="k2")
    )
    user_prompt = service.llm.calls[1][1]["content"]
    assert "这是回答。" in user_prompt


def test_memory_rolls_up_summary(tmp_path):
    cfg = Settings(api_key="k", db_path=tmp_path / "m.db", summary_trigger=3, summary_max_chars=100)
    store = Store(cfg.db_path)
    memory = Memory(store, cfg)
    for i in range(3):
        memory.record("room1", f"第{i}条消息", sender_name="张三")

    assert memory.maybe_summarize("room1", FakeLLM("压缩后的摘要")) is True
    summary, last_rowid = store.get_summary("room1")
    assert summary == "压缩后的摘要"
    assert last_rowid == 3
    # 没攒够就不该再压缩
    assert memory.maybe_summarize("room1", FakeLLM("再来一次")) is False