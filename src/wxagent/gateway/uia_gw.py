# -*- coding: utf-8 -*-
"""微信 4.x 网关：Windows UI Automation 实现。

和 wcferry 网关的区别：不注入、不改客户端，靠无障碍接口读写界面。
代价是微信必须保持窗口打开、且界面结构变了要跟着改选择器。

实测到的界面事实（微信 4.1.15.56）：
- 会话列表  [ListControl] name='会话' class='mmui::XTableView'
            子项 [ListItemControl] class='mmui::ChatSessionCell'
            名字 = "会话名\n未读数或摘要\n时间"
- 消息列表  [ListControl] name='消息' class='mmui::RecyclerListView'
            文本消息 [ListItemControl] class='mmui::ChatTextItemView'，正文就是 Name
- 输入框    [EditControl] class='mmui::ChatInputField'
- 发送按钮  [ButtonControl] name='发送' class='mmui::XOutlineButton'
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import uiautomation as auto

from ..accessibility import AccessibilityGate
from .base import BaseGateway, MessageHandler

log = logging.getLogger(__name__)

SESSION_LIST_NAME = "会话"
MESSAGE_LIST_NAME = "消息"
SESSION_CELL_CLASS = "mmui::ChatSessionCell"
TEXT_ITEM_CLASS = "mmui::ChatTextItemView"
INPUT_FIELD_CLASS = "mmui::ChatInputField"
SEND_BUTTON_NAME = "发送"
MAIN_WINDOW_CLASS = "mmui::MainWindow"

# 一条会话在列表里最多带几个字段
MAX_TEXT_WINDOW = 8


@dataclass(slots=True)
class SessionInfo:
    title: str
    unread: int
    summary: str
    time_text: str

    @property
    def has_new(self) -> bool:
        return self.unread > 0


def parse_session_name(raw: str) -> SessionInfo:
    """解析会话列表项的 Name。

    实测格式："会话名\n未读数或摘要\n时间"，空字段会保留为 ''。
    """
    parts = [p.strip() for p in (raw or "").split("\n")]
    parts = [p for p in parts if p] or [""]
    title = parts[0]
    unread = 0
    summary = ""
    time_text = ""

    for part in parts[1:]:
        if part.isdigit():
            unread = int(part)
            continue
        # 形如 "[3条] 摘要"
        if part.startswith("[") and "条]" in part:
            digits = part[1:part.index("条]")]
            if digits.isdigit():
                unread = int(digits)
            rest = part[part.index("条]") + 2:].strip()
            if rest:
                summary = rest
            continue
        if ":" in part and len(part) <= 6 and part.replace(":", "").isdigit():
            time_text = part
            continue
        if not summary:
            summary = part
        else:
            time_text = part

    return SessionInfo(title=title, unread=unread, summary=summary, time_text=time_text)


def contains_mention(text: str, bot_names: list[str]) -> bool:
    """是否 @ 了机器人（微信 @ 后跟 U+2005 窄空格）。"""
    if not text:
        return False
    for name in bot_names:
        if name and f"@{name}" in text:
            return True
    return False


def is_at_all(text: str) -> bool:
    return any(token in (text or "") for token in ("@所有人", "@All", "@all"))


class UiaGateway(BaseGateway):
    def __init__(
        self,
        handler: MessageHandler,
        *,
        bot_names: list[str],
        poll_interval: float = 3.0,
        allowed_chats: list[str] | None = None,
    ) -> None:
        super().__init__(handler)
        self.bot_names = [n for n in bot_names if n]
        self.poll_interval = max(1.0, float(poll_interval))
        self.allowed_chats = [c for c in (allowed_chats or []) if c]

        self._gate = AccessibilityGate()
        self._window = None
        self._running = False
        self._answered: dict[str, str] = {}
        self._self_title = ""

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if not self._gate.start():
            raise SystemExit(
                "无法激活微信的无障碍控件树。请确认：\n"
                "  1) 微信 4.x 已登录并保持窗口打开\n"
                "  2) 本程序与微信权限级别一致（都不提权）\n"
                "  3) 微信版本为 4.1.x"
            )
        self._window = auto.WindowControl(Name="微信", ClassName=MAIN_WINDOW_CLASS)
        if not self._window.Exists(10):
            raise SystemExit("控件树已激活，但没找到 mmui::MainWindow")
        try:
            self._window.SetActive()
        except Exception:
            log.debug("置前失败，继续", exc_info=True)
        log.info("微信网关就绪（UI Automation）")
        self._self_title = self._read_self_title()

    def _read_self_title(self) -> str:
        try:
            bar = self._window.Control(ClassName="mmui::MainTabBar")
            for item in bar.GetChildren():
                if item.Name == "微信":
                    return "微信"
        except Exception:
            pass
        return ""

    def run_forever(self) -> None:
        if self._window is None:
            self.start()
        self._running = True
        log.info("开始监听微信消息…（每 %.1fs 轮询一次）", self.poll_interval)
        while self._running:
            try:
                self._poll_once()
            except Exception:
                log.exception("轮询异常，稍后继续")
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
        self._gate.stop()

    # ---------------------------------------------------------------- 轮询
    def _poll_once(self) -> None:
        for session in self._sessions():
            if not session.has_new:
                continue
            if self.allowed_chats and not any(
                key and key in session.title for key in self.allowed_chats
            ):
                continue
            self._handle_session(session)

    def _sessions(self) -> list[SessionInfo]:
        table = self._window.ListControl(Name=SESSION_LIST_NAME)
        if not table.Exists(2):
            return []
        out: list[SessionInfo] = []
        for cell in table.GetChildren():
            if cell.ClassName != SESSION_CELL_CLASS:
                continue
            out.append(parse_session_name(cell.Name or ""))
        return out

    def _handle_session(self, session: SessionInfo) -> None:
        """有未读的会话：打开 -> 读最后几条 -> 如果是 @ 就回复。"""
        if not self._open_chat(session.title):
            log.warning("打开会话失败: %s", session.title)
            return

        time.sleep(0.4)
        texts = self._recent_texts()
        if not texts:
            return

        last = texts[-1]
        if not contains_mention(last, self.bot_names) or is_at_all(last):
            return
        if self._answered.get(session.title) == last:
            return  # 已经处理过这条，避免重复回复

        log.info("收到 @ 消息 群=%s 内容=%r", session.title, last[:80])
        msg = self._build_incoming(session.title, last, texts)
        replies = self.dispatch(msg)
        if not replies:
            self._answered[session.title] = last
            return

        self.deliver(session.title, replies)
        self._answered[session.title] = last

    def _build_incoming(self, title: str, last: str, texts: list[str]):
        from ..agent import IncomingMessage

        # 群聊上下文：把最近几条拼进 content 之外由核心自己管理，
        # 这里只把最新一条作为问题，并把最近记录塞进 quoted 供模型参考。
        context = "\n".join(texts[:-1][-6:])
        return IncomingMessage(
            chat_id=title,
            chat_name=title,
            content=last,
            sender_name="群成员",
            sender_id="",
            msg_id=f"{title}|{last}",
            ts=int(time.time()),
            is_group=True,
            is_at_me=True,
            kind="text",
            quoted=context,
        )

    def _current_chat(self) -> str:
        """当前打开的会话名：输入框的 Name 就是会话标题。"""
        try:
            edit = self._window.EditControl(ClassName=INPUT_FIELD_CLASS)
            if edit.Exists(0.3):
                return (edit.Name or "").strip()
        except Exception:
            pass
        return ""

    def _open_chat(self, title: str) -> bool:
        # 已经打开就不能再点，否则会 toggle 关闭（实测踩过）
        if self._current_chat() == title:
            return True

        table = self._window.ListControl(Name=SESSION_LIST_NAME)
        if not table.Exists(2):
            return False
        for cell in table.GetChildren():
            name = (cell.Name or "").split("\n")[0].strip()
            if name != title:
                continue
            try:
                cell.Click(simulateMove=True)
            except Exception:
                log.debug("点击会话失败", exc_info=True)
                return False
            # 等界面切换完成
            for _ in range(10):
                if self._current_chat() == title:
                    return True
                time.sleep(0.15)
            return self._current_chat() == title
        log.warning("会话列表里没有: %s", title)
        return False

    def _recent_texts(self) -> list[str]:
        lst = self._window.ListControl(Name=MESSAGE_LIST_NAME)
        if not lst.Exists(2):
            return []
        texts = []
        for item in lst.GetChildren():
            if item.ClassName != TEXT_ITEM_CLASS:
                continue
            content = (item.Name or "").strip()
            if content:
                texts.append(content)
        return texts[-MAX_TEXT_WINDOW:]

    # ---------------------------------------------------------------- 发送
    def send_text(self, chat_id: str, text: str) -> bool:
        if not text.strip():
            return False
        if not self._open_chat(chat_id):
            return False
        try:
            edit = self._window.EditControl(ClassName=INPUT_FIELD_CLASS)
            if not edit.Exists(3):
                log.error("找不到输入框")
                return False

            edit.Click(simulateMove=True)
            time.sleep(0.15)
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys("{Delete}", waitTime=0.05)

            self._set_clipboard(text)
            auto.SendKeys("{Ctrl}v", waitTime=0.08)
            time.sleep(0.3)

            if self._input_value() == "":
                log.error("粘贴失败，输入框仍为空")
                return False

            # 发送：优先点按钮，不行再回车；每次都用输入框是否清空来验证
            button = self._window.ButtonControl(Name=SEND_BUTTON_NAME)
            if button.Exists(1):
                try:
                    button.Click(simulateMove=True)
                    time.sleep(0.35)
                except Exception:
                    log.debug("点击发送按钮失败", exc_info=True)
            if self._input_value() != "":
                auto.SendKeys("{Enter}", waitTime=0.05)
                time.sleep(0.35)
            if self._input_value() != "":
                log.error("发送后输入框仍有内容，判定发送失败")
                return False
            return True
        except Exception:
            log.exception("发送失败")
            return False

    def _input_value(self) -> str:
        try:
            edit = self._window.EditControl(ClassName=INPUT_FIELD_CLASS)
            if not edit.Exists(0.3):
                return ""
            return (edit.GetValuePattern().Value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _set_clipboard(text: str) -> None:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()