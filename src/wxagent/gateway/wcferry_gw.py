# -*- coding: utf-8 -*-
"""微信个人号网关（WeChatFerry 实现）。

这一层会通过 wcf.exe 把 spy.dll 注入 WeChat.exe 进程，
因此只能在 Windows 上跑，且必须是它支持的微信版本（本项目以 3.9.12.17 验证）。

设计要点：
- 只处理文本和"引用回复"，图片/语音等直接跳过，避免模型瞎编图片内容；
- 发送失败不重试（微信侧重试容易触发风控），只记日志；
- 所有 wxid -> 昵称的查询结果都缓存，减少 DB 查询次数。
"""
from __future__ import annotations

import logging
import re
import time
from queue import Empty

from ..agent import IncomingMessage
from .base import BaseGateway, MessageHandler

log = logging.getLogger(__name__)

# 微信消息类型 -> 内部记号（1=文本，49=app 消息，其余为富媒体）
KIND_BY_TYPE = {
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接",
    10000: "系统消息",
    10002: "撤回消息",
}

# 群消息的 content 偶尔会带 "wxid_xxx:\n" 前缀
_SENDER_PREFIX = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]{4,}@chatroom):\n")
_CDATA = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)


def _unescape_cdata(value: str) -> str:
    value = (value or "").strip()
    matched = _CDATA.match(value)
    return matched.group(1).strip() if matched else value


def _sql_escape(value: str) -> str:
    return (value or "").replace("'", "''")


class WcferryGateway(BaseGateway):
    def __init__(self, handler: MessageHandler, *, port: int = 10086, debug: bool = False) -> None:
        super().__init__(handler)

        try:
            from wcferry import Wcf  # 延迟导入：非 Windows 环境也要能 import 本模块
        except ImportError as exc:
            raise SystemExit(
                "缺少 wcferry。请在装微信的 Windows 上执行：pip install -r requirements-gateway.txt"
            ) from exc

        self._wcf = Wcf(port=port, debug=debug)
        self._self_wxid = self._wcf.get_self_wxid()
        self._self_names = self._read_self_names()
        self._name_cache: dict[str, str] = {}
        self._room_cache: dict[str, str] = {}
        self._running = False
        log.info("微信网关就绪：%s (%s)", "/".join(self._self_names), self._self_wxid)

    # ---------------------------------------------------------------- 身份
    def _read_self_names(self) -> list[str]:
        names: list[str] = []
        try:
            info = self._wcf.get_user_info() or {}
            if info.get("name"):
                names.append(str(info["name"]).strip())
        except Exception as exc:
            log.warning("读取登录账号信息失败: %s", exc)

        if not names:
            try:
                rows = self._wcf.query_sql(
                    "MicroMsg.db",
                    f"SELECT NickName FROM Contact WHERE UserName = '{_sql_escape(self._self_wxid)}';",
                )
                if rows and rows[0].get("NickName"):
                    names.append(str(rows[0]["NickName"]).strip())
            except Exception as exc:
                log.warning("从通讯录读取昵称失败: %s", exc)

        return names or [self._self_wxid]

    def self_names(self) -> list[str]:
        return list(self._self_names)

    def self_wxid(self) -> str:
        return self._self_wxid

    # ---------------------------------------------------------------- 主循环
    def run_forever(self) -> None:
        if not self._wcf.is_login():
            raise SystemExit("微信还没有登录，请先在这台机器上登录微信，再重新启动。")

        if not self._wcf.enable_receiving_msg():
            raise SystemExit("开启消息接收失败：请确认微信版本是 3.9.12.17，并尝试以管理员身份运行。")

        self._running = True
        log.info("开始监听微信消息…（Ctrl+C 退出）")

        while self._running:
            try:
                raw = self._wcf.get_msg(block=True)
            except Empty:
                continue
            except Exception:
                if not self._running:
                    break
                log.exception("读取消息异常，1 秒后继续")
                time.sleep(1)
                continue

            try:
                self._handle_raw(raw)
            except Exception:
                log.exception("处理消息异常，已跳过")

    def _handle_raw(self, raw) -> None:
        msg = self._to_incoming(raw)
        if msg is None:
            return

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "收到群=%s 发送者=%s @我=%s 内容=%r",
                msg.chat_name, msg.sender_name, msg.is_at_me, msg.content[:120],
            )

        replies = self.dispatch(msg)
        if replies:
            self.deliver(msg.chat_id, replies)

    # ---------------------------------------------------------------- 消息解析
    def _to_incoming(self, raw) -> IncomingMessage | None:
        if raw.from_self():
            return None

        msg_type = int(getattr(raw, "type", 0) or 0)
        if msg_type in (10000, 10002):
            return None  # 系统提示 / 撤回，别当上下文

        is_group = bool(raw.from_group())
        roomid = (raw.roomid or "") if is_group else ""
        chat_id = roomid or raw.sender
        if not chat_id:
            return None

        text = ""
        quoted = ""
        if msg_type == 1:
            text = self._clean_content(raw.content)
        elif msg_type == 49:
            title, quoted = self._parse_appmsg(raw.xml)
            text = self._clean_content(raw.content) or title
            if not text and not quoted:
                return None
        else:
            log.debug("跳过非文本消息 type=%s (%s)", msg_type, KIND_BY_TYPE.get(msg_type, "未知"))
            return None

        if not text.strip():
            return None

        sender_id = raw.sender or ""
        sender_name = self._display_name(sender_id, roomid)
        chat_name = self._room_name(roomid) if is_group else sender_name

        return IncomingMessage(
            chat_id=chat_id,
            content=text,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_name=chat_name,
            msg_id=str(raw.id or ""),
            ts=int(raw.ts or time.time()),
            is_group=is_group,
            is_at_me=self._is_at_me(raw, text),
            is_self=False,
            kind="text",
            quoted=quoted,
        )

    @staticmethod
    def _clean_content(content: str) -> str:
        return _SENDER_PREFIX.sub("", (content or "").strip()).strip()

    def _is_at_me(self, raw, text: str) -> bool:
        # wcferry 的 is_at 读 xml 里的 <atuserlist>，比匹配文本可靠
        try:
            if raw.is_at(self._self_wxid):
                return True
        except Exception as exc:
            log.debug("is_at 判定失败: %s", exc)

        # 兜底：xml 里没有清单时按昵称匹配
        return any(f"@{name}" in text for name in self._self_names if name)

    @classmethod
    def _parse_appmsg(cls, xml_text: str) -> tuple[str, str]:
        """解析引用回复：返回 (本条文字, 被引用原文)。"""
        if not xml_text:
            return "", ""

        title = ""
        matched = re.search(r"<title>(.*?)</title>", xml_text, re.S)
        if matched:
            title = _unescape_cdata(matched.group(1))

        quoted = ""
        refer = re.search(r"<refermsg>(.*?)</refermsg>", xml_text, re.S)
        if refer:
            content = re.search(r"<content>(.*?)</content>", refer.group(1), re.S)
            if content:
                value = _unescape_cdata(content.group(1))
                if value.startswith("<"):
                    inner = re.search(r"<title>(.*?)</title>", value, re.S)
                    value = _unescape_cdata(inner.group(1)) if inner else ""
                quoted = value.replace("\n", " ").strip()[:200]

        return title[:500], quoted

    # ---------------------------------------------------------------- 名称解析
    def _display_name(self, wxid: str, roomid: str) -> str:
        if not wxid:
            return "群友"
        key = f"{roomid}|{wxid}"
        cached = self._name_cache.get(key)
        if cached:
            return cached

        name = ""
        try:
            if roomid:
                name = self._wcf.get_alias_in_chatroom(wxid, roomid) or ""
            if not name:
                rows = self._wcf.query_sql(
                    "MicroMsg.db",
                    f"SELECT NickName FROM Contact WHERE UserName = '{_sql_escape(wxid)}';",
                )
                if rows:
                    name = str(rows[0].get("NickName") or "")
        except Exception as exc:
            log.debug("查询昵称失败 %s: %s", wxid, exc)

        name = name.strip() or wxid
        self._name_cache[key] = name
        return name

    def _room_name(self, roomid: str) -> str:
        if not roomid:
            return ""
        cached = self._room_cache.get(roomid)
        if cached:
            return cached

        name = ""
        try:
            rows = self._wcf.query_sql(
                "MicroMsg.db",
                f"SELECT NickName FROM Contact WHERE UserName = '{_sql_escape(roomid)}';",
            )
            if rows:
                name = str(rows[0].get("NickName") or "")
        except Exception as exc:
            log.debug("查询群名失败 %s: %s", roomid, exc)

        name = name.strip() or roomid
        self._room_cache[roomid] = name
        return name

    # ---------------------------------------------------------------- 发送
    def send_text(self, chat_id: str, text: str) -> bool:
        try:
            status = self._wcf.send_text(text, chat_id)
        except Exception as exc:
            log.error("发送异常 chat=%s: %s", chat_id, exc)
            return False

        if status != 0:
            log.error("发送失败 chat=%s status=%s", chat_id, status)
            return False

        time.sleep(0.4)  # 连发多条时给微信留点间隔
        return True

    def stop(self) -> None:
        self._running = False
        try:
            self._wcf.disable_recv_msg()
        except Exception:
            pass
        try:
            self._wcf.cleanup()
        except Exception:
            pass
        log.info("微信网关已停止")