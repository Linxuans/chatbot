# -*- coding: utf-8 -*-
"""UIA 网关的纯函数测试（不依赖正在运行的微信）。"""
from __future__ import annotations

import pytest

pytest.importorskip("uiautomation", reason="仅在 Windows 且装了 uiautomation 时运行")

from wxagent.gateway.uia_gw import contains_mention, is_at_all, parse_session_name


def test_parse_group_session_with_summary():
    info = parse_session_name("浮三生、再睡一夏夏\n浮三生: @城南\u2005在\n23:45")
    assert info.title == "浮三生、再睡一夏夏"
    assert info.unread == 0
    assert "浮三生" in info.summary
    assert info.time_text == "23:45"


def test_parse_session_with_unread_count():
    info = parse_session_name("再睡一夏夏\n1\n23:43")
    assert info.title == "再睡一夏夏"
    assert info.unread == 1
    assert info.has_new is True


def test_parse_official_account_with_bracket_count():
    info = parse_session_name("腾讯新闻\n[3条] \n天津市落地住房公积金惠民新政\n23:29")
    assert info.title == "腾讯新闻"
    assert info.unread == 3
    assert "公积金" in info.summary


def test_parse_empty_fields():
    info = parse_session_name("文件传输助手\n\n\n")
    assert info.title == "文件传输助手"
    assert info.unread == 0


def test_contains_mention_narrow_space():
    assert contains_mention("@城南\u2005在", ["城南"]) is True
    assert contains_mention("@城南\u2005在", ["其他人"]) is False
    assert contains_mention("没有@任何人", ["城南"]) is False


def test_at_all_detection():
    assert is_at_all("@所有人 通知") is True
    assert is_at_all("@All 通知") is True
    assert is_at_all("@城南 在") is False