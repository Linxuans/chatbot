# -*- coding: utf-8 -*-
"""流水线单测：不需要微信、不需要网络、不需要 API key。"""
from __future__ import annotations

from wxagent.pipeline import (
    Cooldown,
    clean_text,
    detect_trigger,
    sanitize_reply,
    split_reply,
    strip_bot_mentions,
)


def test_clean_text_normalises_wechat_space():
    assert clean_text("@小助手\u2005你好") == "@小助手 你好"
    assert clean_text("  a\r\nb  ") == "a\nb"


def test_strip_bot_mentions_removes_nickname_and_leading_at():
    assert strip_bot_mentions("@小助手 今天几号", ["小助手"]) == "今天几号"
    assert strip_bot_mentions("@张三 帮我看下", ["小助手"]) == "帮我看下"


def test_detect_at_by_flag():
    hit, body = detect_trigger(
        "@小助手 今天几号",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=True, is_at_me=True, private_ok=True,
    )
    assert hit is True
    assert body == "今天几号"


def test_detect_at_by_nickname_without_flag():
    hit, body = detect_trigger(
        "@小助手 在吗",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=True, is_at_me=False, private_ok=True,
    )
    assert hit is True
    assert body == "在吗"


def test_detect_at_with_narrow_space():
    hit, body = detect_trigger(
        "@小助手\u2005价格多少",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=True, is_at_me=True, private_ok=True,
    )
    assert hit is True
    assert body == "价格多少"


def test_no_trigger_for_plain_group_message():
    hit, _ = detect_trigger(
        "今天中午吃什么",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=True, is_at_me=False, private_ok=True,
    )
    assert hit is False


def test_prefix_mode():
    hit, body = detect_trigger(
        "/ask 明天天气",
        mode="prefix", prefix="/ask", bot_names=["小助手"],
        is_group=True, is_at_me=False, private_ok=True,
    )
    assert hit is True
    assert body == "明天天气"


def test_private_message_respects_switch():
    hit, _ = detect_trigger(
        "在吗",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=False, is_at_me=False, private_ok=True,
    )
    assert hit is True
    hit2, _ = detect_trigger(
        "在吗",
        mode="at", prefix="/", bot_names=["小助手"],
        is_group=False, is_at_me=False, private_ok=False,
    )
    assert hit2 is False


def test_sanitize_removes_markdown():
    raw = "**重点**：明天带伞\n- 早到\n- 带电脑\n`code`\n```python\nprint(1)\n```"
    out = sanitize_reply(raw)
    assert "**" not in out
    assert "`" not in out
    assert "- 早到" not in out
    assert "· 早到" in out
    assert "重点" in out


def test_sanitize_strips_answer_prefix():
    assert sanitize_reply("回答：明天是周二").startswith("明天")


def test_split_reply_respects_limit_and_keeps_text():
    text = "这是一句测试。" * 30
    chunks = split_reply(text, 50)
    assert len(chunks) > 1
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks) == text


def test_split_reply_short_text_untouched():
    assert split_reply("很短", 100) == ["很短"]
    assert split_reply("", 100) == []


def test_cooldown_blocks_second_message():
    guard = Cooldown(cooldown_seconds=30, per_minute=0)
    assert guard.check("room1")[0] is True
    ok, reason = guard.check("room1")
    assert ok is False and reason == "cooldown"
    assert guard.check("room2")[0] is True


def test_cooldown_rate_limit():
    guard = Cooldown(cooldown_seconds=0, per_minute=2)
    assert guard.check("r")[0] is True
    assert guard.check("r")[0] is True
    ok, reason = guard.check("r")
    assert ok is False and reason == "rate-limit"