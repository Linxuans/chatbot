# -*- coding: utf-8 -*-
"""微信 4.x 网关：Windows UI Automation 实现。

与 wcferry 的区别：不注入、不改客户端，靠无障碍接口读写界面。
代价是微信必须保持窗口可用（最小化也不行），且界面改版要跟着改选择器。

实测界面事实（微信 4.1.15.56）：
- 会话列表  [ListControl] name='会话' class='mmui::XTableView'
            子项 [ListItemControl] class='mmui::ChatSessionCell'
            名字 = "会话名\n未读数或摘要\n时间"
- 消息列表  [ListControl] name='消息' class='mmui::RecyclerListView'
            文本消息 [ListItemControl] class='mmui::ChatTextItemView'，正文就是 Name
- 输入框    [EditControl] class='mmui::ChatInputField'
- 发送按钮  [ButtonControl] name='发送' class='mmui::XOutlineButton'

三条工程约束及对策：
1. 微信重启后 gate 失效 -> 每轮检测进程/窗口，自动重新 attach
2. 窗口最小化后读不到 -> 自动还原（不抢焦点，除非真要点击）
3. 打开会话会标记已读 -> 用会话摘要判断 @，不碰未读数，只有真 @ 才开会话
"""
from __future__ import annotations

import ctypes
import logging
import re
import time
from dataclasses import dataclass

import uiautomation as auto

from ..accessibility import AccessibilityGate, find_wechat_window, restore_window, window_state
from .base import BaseGateway, MessageHandler

log = logging.getLogger(__name__)

SESSION_LIST_NAME = "会话"
MESSAGE_LIST_NAME = "消息"
SESSION_CELL_CLASS = "mmui::ChatSessionCell"
TEXT_ITEM_CLASS = "mmui::ChatTextItemView"
INPUT_FIELD_CLASS = "mmui::ChatInputField"
SEND_BUTTON_NAME = "发送"
MAIN_WINDOW_NAME = "微信"

MAX_TEXT_WINDOW = 8


@dataclass(slots=True)
class SessionInfo:
    title: str
    unread: int
    summary: str
    time_text: str
    at_me: bool = False

    @property
    def has_new(self) -> bool:
        return self.unread > 0


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_COUNT_RE = re.compile(r"^\[(\d+)条\]\s*(.*)$")
# 微信在会话行里插入的各种标记，不是消息内容
_MARKERS = {"[有人@我]", "消息免打扰", "[草稿]", "[未读]", "[语音]", "[图片]"}


def parse_session_name(raw: str) -> SessionInfo:
    """解析会话列表项的 Name。

    实测格式（字段数不固定）：
        浮三生、再睡一夏夏\n[7条] \n[有人@我]\n再睡一夏夏: @城南 在\n23:57
        文件传输助手\n\n
        腾讯新闻\n[3条] \n摘要...\n23:29
    """
    raw = raw or ""
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    title = parts[0] if parts else ""

    unread = 0
    time_text = ""
    candidates: list[str] = []

    for part in parts[1:]:
        if part.isdigit():
            unread = max(unread, int(part))
            continue
        counted = _COUNT_RE.match(part)
        if counted:
            unread = max(unread, int(counted.group(1)))
            if counted.group(2).strip():
                candidates.append(counted.group(2).strip())
            continue
        if _TIME_RE.match(part):
            time_text = part
            continue
        if part in _MARKERS:
            continue
        candidates.append(part)

    # 摘要取最后一个候选：微信把最新一条消息放在时间之前
    summary = candidates[-1] if candidates else ""
    return SessionInfo(
        title=title,
        unread=unread,
        summary=summary,
        time_text=time_text,
        at_me="[有人@我]" in raw,
    )


def contains_mention(text: str, bot_names: list[str]) -> bool:
    """是否 @ 了机器人（微信 @ 后跟 U+2005 窄空格）。"""
    if not text:
        return False
    return any(name and f"@{name}" in text for name in bot_names)


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
        auto_restore: bool = True,
    ) -> None:
        super().__init__(handler)
        self.bot_names = [n for n in bot_names if n]
        self.poll_interval = max(1.0, float(poll_interval))
        self.allowed_chats = [c for c in (allowed_chats or []) if c]
        self.auto_restore = auto_restore

        self._gate = AccessibilityGate()
        self._window = None
        self._hwnd = 0
        self._running = False
        self._answered: dict[str, str] = {}
        self._last_summary: dict[str, str] = {}
        self._last_wait_log = 0.0

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        ready = self._gate.start()
        if ready and self._bind_window():
            self._prune_baseline()
            log.info("微信网关就绪（UI Automation），机器人昵称: %s", " / ".join(self.bot_names))
            return
        log.warning("微信尚未就绪（未启动 / 未登录 / 窗口未加载），进入等待模式")
        log.warning("网关会一直重试；扫码登录成功后会自动接上，无需重启本程序")

    def _bind_window(self) -> bool:
        """绑定主窗口；窗口被最小化/隐藏时先还原。"""
        found = find_wechat_window()
        if not found:
            return False
        hwnd, _pid, _title = found
        visible, minimised = window_state(hwnd)
        if (not visible or minimised) and self.auto_restore:
            restore_window(hwnd, foreground=False)

        window = auto.WindowControl(Name=MAIN_WINDOW_NAME, searchDepth=1)
        if not window.Exists(3):
            return False
        self._window = window
        self._hwnd = hwnd
        return True

    def _ensure_ready(self) -> bool:
        """保证：gate 开着 + 窗口还在 + 窗口可读。"""
        if not self._gate.ensure():
            return False
        found = find_wechat_window()
        if not found:
            now = time.time()
            if now - self._last_wait_log > 60:
                log.warning("微信窗口不可用（未登录 / 已关闭），等待中…")
                self._last_wait_log = now
            return False
        hwnd, pid, _ = found
        if pid != self._gate.pid or self._window is None or hwnd != self._hwnd:
            log.info("检测到微信窗口变化（pid=%s hwnd=%s），重新绑定", pid, hwnd)
            if not self._bind_window():
                return False
            self._prune_baseline()

        visible, minimised = window_state(hwnd)
        if (not visible or minimised) and self.auto_restore:
            restore_window(hwnd, foreground=False)
        return True

    def _prune_baseline(self) -> None:
        """记录当前会话摘要，避免启动时回复历史消息。

        例外：带「有人@我」标记且未读的会话不记基线——那是明确找过机器人、
        但还没被回复的消息，启动后应当补上。
        """
        for session in self._sessions():
            if session.at_me and session.unread > 0:
                log.info("启动时发现未回复的 @ 会话: %s（未读 %d）", session.title, session.unread)
                continue
            self._last_summary.setdefault(session.title, session.summary)

    def run_forever(self) -> None:
        if self._window is None:
            self.start()
        self._running = True
        log.info("开始监听微信消息…（每 %.1fs 轮询一次）", self.poll_interval)
        while self._running:
            try:
                if self._ensure_ready():
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
            if self.allowed_chats and not any(k and k in session.title for k in self.allowed_chats):
                continue

            previous = self._last_summary.get(session.title)
            if session.summary == previous:
                continue
            self._last_summary[session.title] = session.summary

            # 两个信号任一命中就去读：微信自己的「有人@我」标记，或摘要里出现 @机器人
            mentioned = contains_mention(session.summary, self.bot_names)
            if not (session.at_me or mentioned):
                continue

            self._handle_session(session)

    def _sessions(self) -> list[SessionInfo]:
        if self._window is None:
            return []
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
        if not self._open_chat(session.title):
            log.warning("打开会话失败: %s", session.title)
            return

        time.sleep(0.4)
        texts = self._recent_texts()
        target = next(
            (t for t in reversed(texts)
             if contains_mention(t, self.bot_names) and not is_at_all(t)),
            "",
        )
        if not target:
            return
        if self._answered.get(session.title) == target:
            return

        log.info("收到 @ 消息 群=%s 内容=%r", session.title, target[:80])
        self._answered[session.title] = target

        msg = self._build_incoming(session.title, target, texts)
        replies = self.dispatch(msg)
        if not replies:
            return
        self.deliver(session.title, replies)

    def _build_incoming(self, title: str, last: str, texts: list[str]):
        from ..agent import IncomingMessage

        context = "\n".join(t for t in texts if t != last)
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
            quoted=context[:800],
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

    def _chat_is_open(self, title: str) -> bool:
        """输入框的 Name 是「会话标题 + 占位提示」，例如「群名长按鼠标 语音输入」。

        所以不能用相等判断，否则会误判成「没打开」而重复点击、把会话 toggle 关闭。
        """
        current = self._current_chat()
        if not current:
            return False
        return current == title or current.startswith(title)

    def _open_chat(self, title: str) -> bool:
        # 已经打开就不能再点，否则会 toggle 关闭（实测踩过）
        if self._chat_is_open(title):
            return True

        table = self._window.ListControl(Name=SESSION_LIST_NAME)
        if not table.Exists(2):
            return False
        for cell in table.GetChildren():
            name = (cell.Name or "").split("\n")[0].strip()
            if name != title:
                continue
            try:
                restore_window(self._hwnd, foreground=True)
                cell.Click(simulateMove=True)
            except Exception:
                log.debug("点击会话失败", exc_info=True)
                return False
            for _ in range(12):
                if self._chat_is_open(title):
                    return True
                time.sleep(0.15)
            return self._chat_is_open(title)
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
    def _input_value(self) -> str:
        try:
            edit = self._window.EditControl(ClassName=INPUT_FIELD_CLASS)
            if not edit.Exists(0.3):
                return ""
            return (edit.GetValuePattern().Value or "").strip()
        except Exception:
            return ""

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

    @staticmethod
    def _set_clipboard(text: str) -> None:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()