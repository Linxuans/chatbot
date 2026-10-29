# -*- coding: utf-8 -*-
"""搜索解析的纯函数测试（不联网）。"""
from __future__ import annotations

from wxagent.tools import format_results, parse_bing, run_tool

SAMPLE = """
<li class="b_algo"><h2><a href="https://example.com/a">上海天气 &amp; 预报</a></h2>
<p>今天上海多云，最高 26 度。</p></li>
<li class="b_algo"><h2><a href="https://example.com/b">另一个结果</a></h2>
<p>第二条摘要。</p></li>
"""


def test_parse_bing_extracts_fields():
    results = parse_bing(SAMPLE)
    assert len(results) == 2
    assert results[0].title == "上海天气 & 预报"
    assert results[0].url == "https://example.com/a"
    assert "26 度" in results[0].snippet


def test_parse_bing_respects_limit():
    assert len(parse_bing(SAMPLE, limit=1)) == 1


def test_parse_bing_handles_garbage():
    assert parse_bing("") == []
    assert parse_bing("<html>no results</html>") == []


def test_format_results_empty():
    assert "没有搜到" in format_results([])


def test_run_tool_unknown():
    assert "未知工具" in run_tool("nope", {})