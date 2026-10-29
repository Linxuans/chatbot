# -*- coding: utf-8 -*-
"""消息处理流水线：触发判定、文本清洗、分片、限流。

这里全是纯函数和一个小状态类，不依赖微信也不依赖网络，方便单测。
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Iterable

# 微信 @ 提及时后面跟的是 U+2005（四分之一全角空格），不是普通空格
NARROW_SPACE = "\u2005"

_LEADING_AT = re.compile(r"^\s*@[^\s\u2005]{1,40}[\s\u2005]*")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")
_MD_FENCE = re.compile(r"```[a-zA-Z0-9_+\-]*\n?")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_UNDER = re.compile(r"__(.+?)__")
_MD_HEAD = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_CODE = re.compile(r"`([^`]*)`")
_MD_QUOTE = re.compile(r"(?m)^\s{0,3}>\s?")
_MD_BULLET = re.compile(r"(?m)^\s*[-*+]\s+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HR = re.compile(r"(?m)^\s*([-*_])\1{2,}\s*$")


def clean_text(text: str) -> str:
    """统一换行、去掉零宽字符和微信的窄空格。"""
    out = (text or "").replace("\u2005", " ").replace("\u200b", "").replace("\ufeff", "")
    return out.replace("\r\n", "\n").replace("\r", "\n").strip()


def strip_bot_mentions(text: str, bot_names: Iterable[str]) -> str:
    out = clean_text(text)
    names = sorted({n.strip() for n in bot_names if n and n.strip()}, key=len, reverse=True)
    for name in names:
        out = out.replace(f"@{name}", " ")
    out = _LEADING_AT.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def detect_trigger(
    text: str,
    *,
    mode: str,
    prefix: str,
    bot_names: Iterable[str],
    is_group: bool,
    is_at_me: bool,
    private_ok: bool,
) -> tuple[bool, str]:
    """返回 (是否该回复, 去掉 @ 之后的正文)。"""
    names = [n for n in bot_names if n]
    cleaned = strip_bot_mentions(text, names)

    if not is_group:
        return (bool(private_ok and cleaned), cleaned)

    if mode == "all":
        return (bool(cleaned), cleaned)

    if mode == "prefix":
        mark = prefix or "/"
        if not cleaned.startswith(mark):
            return False, cleaned
        return True, cleaned[len(mark):].strip()

    # 默认 at 模式：微信的 @ 有两种表现，都认
    hit = bool(is_at_me) or any(f"@{name}" in clean_text(text) for name in names)
    return (hit and bool(cleaned), cleaned)


def sanitize_reply(text: str) -> str:
    """把模型输出改成适合微信显示的纯文本（微信不渲染 Markdown）。"""
    out = clean_text(text)
    if not out:
        return ""

    out = _MD_FENCE.sub("", out).replace("```", "")
    out = _MD_BOLD.sub(r"\1", out)
    out = _MD_UNDER.sub(r"\1", out)
    out = _MD_HEAD.sub("", out)
    out = _MD_CODE.sub(r"\1", out)
    out = _MD_QUOTE.sub("", out)
    out = _MD_BULLET.sub("· ", out)
    out = _MD_LINK.sub(r"\1", out)
    out = _MD_HR.sub("", out)

    # 去掉「回答：」「答：」这类多余前缀
    out = re.sub(r"^\s*(回答|答复|答|回复)\s*[:：]\s*", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _hard_split(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def split_reply(text: str, limit: int) -> list[str]:
    """微信单条消息有长度感受上限，长回答按段落/句子切开分条发。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(50, int(limit))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""

    for paragraph in text.split("\n"):
        candidate = f"{buf}\n{paragraph}" if buf else paragraph
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(paragraph) <= limit:
            buf = paragraph
            continue

        for sentence in _SENTENCE_END.split(paragraph):
            if not sentence:
                continue
            candidate = f"{buf}{sentence}"
            if len(candidate) <= limit:
                buf = candidate
                continue
            if buf:
                chunks.append(buf)
                buf = ""
            if len(sentence) <= limit:
                buf = sentence
            else:
                pieces = _hard_split(sentence, limit)
                chunks.extend(pieces[:-1])
                buf = pieces[-1]

    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


class Cooldown:
    """每群独立：最小间隔 + 每分钟条数上限。

    这两道闸主要是为了降低被微信风控盯上的概率，不是为了省 token。
    """

    def __init__(self, cooldown_seconds: float = 3.0, per_minute: int = 6) -> None:
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.per_minute = max(0, int(per_minute))
        self._last: dict[str, float] = {}
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, chat_id: str) -> tuple[bool, str]:
        """(是否放行, 原因)。放行时会记一次。"""
        now = time.monotonic()
        with self._lock:
            last = self._last.get(chat_id)
            if last is not None and now - last < self.cooldown_seconds:
                return False, "cooldown"
            hits = self._hits.setdefault(chat_id, deque())
            while hits and now - hits[0] > 60.0:
                hits.popleft()
            if self.per_minute and len(hits) >= self.per_minute:
                return False, "rate-limit"
            hits.append(now)
            self._last[chat_id] = now
            return True, ""

    def peek(self, chat_id: str) -> bool:
        """只看是否会被拦，不记录。"""
        now = time.monotonic()
        with self._lock:
            last = self._last.get(chat_id)
            if last is not None and now - last < self.cooldown_seconds:
                return False
            hits = self._hits.get(chat_id)
            if hits:
                while hits and now - hits[0] > 60.0:
                    hits.popleft()
                if self.per_minute and len(hits) >= self.per_minute:
                    return False
            return True