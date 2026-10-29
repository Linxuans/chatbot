# -*- coding: utf-8 -*-
"""自检模块的纯函数测试（不依赖真实微信环境）。"""
from __future__ import annotations

import socket

from wxagent.config import Settings
from wxagent.doctor import WCFERRY_TO_WEIXIN, _check_api_call, _port_free, _version_tuple


def test_version_tuple_parses():
    assert _version_tuple("3.9.12.17") == (3, 9, 12, 17)
    assert _version_tuple("") == ()
    assert _version_tuple("bad") == ()


def test_version_mapping_is_wellformed():
    assert WCFERRY_TO_WEIXIN, "版本对应表不能为空"
    for wcferry_version, weixin_version in WCFERRY_TO_WEIXIN.items():
        assert len(_version_tuple(wcferry_version)) == 4
        assert len(_version_tuple(weixin_version)) == 4


def test_port_free_detects_occupied_port():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        occupied = holder.getsockname()[1]
        assert _port_free(occupied) is False
    finally:
        holder.close()


def test_port_free_flags_released_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert _port_free(port) is True

class _FakeLLM:
    def __init__(self, reply="正常", error=None):
        self.reply = reply
        self.error = error

    def chat(self, messages, **kwargs):
        if self.error:
            raise self.error
        return self.reply


def test_check_api_call_without_key():
    cfg = Settings(api_key="")
    result = _check_api_call(cfg, llm=_FakeLLM())
    assert result.status == "fail"


def test_check_api_call_success():
    cfg = Settings(api_key="sk-test", model="deepseek-chat")
    result = _check_api_call(cfg, llm=_FakeLLM("正常"))
    assert result.status == "ok"
    assert "deepseek-chat" in result.detail


def test_check_api_call_failure_reports_reason():
    cfg = Settings(api_key="sk-bad")
    result = _check_api_call(cfg, llm=_FakeLLM(error=RuntimeError("401 Unauthorized")))
    assert result.status == "fail"
    assert "401" in result.detail
