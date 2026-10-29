# -*- coding: utf-8 -*-
"""给模型用的工具：目前只有联网搜索。

搜索走必应网页版（不需要 API Key）。国内实测 DuckDuckGo 不可达、
百度返回反爬页，必应最稳。
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

BING_URL = "https://www.bing.com/search?q={query}&setlang=zh-CN&count=10"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    content: str = ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", text or ""))).strip()


def parse_bing(page: str, limit: int = 5) -> list[SearchResult]:
    """从必应结果页里提取标题/链接/摘要。纯函数，方便单测。"""
    results: list[SearchResult] = []
    for block in BLOCK_RE.findall(page or ""):
        matched = TITLE_RE.search(block)
        if not matched:
            continue
        url = html.unescape(matched.group(1)).strip()
        title = _clean(matched.group(2))
        if not title or not url:
            continue
        snippet_match = SNIPPET_RE.search(block)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= limit:
            break
    return results


def fetch_text(url: str, max_chars: int = 1200, timeout: float = 10.0) -> str:
    """抓取网页正文纯文本。失败返回空串，不影响搜索本身。"""
    if not url or not url.startswith("http"):
        return ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(600_000)
    except Exception as exc:
        log.debug("抓取失败 %s: %s", url, exc)
        return ""

    page = ""
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            page = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not page:
        return ""

    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    text = html.unescape(TAG_RE.sub(" ", page))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def web_search(
    query: str,
    limit: int = 5,
    timeout: float = 15.0,
    fetch_top: int = 2,
) -> list[SearchResult]:
    """联网搜索；默认再抓前 2 条网页正文，因为必应摘要常常没有具体数据。"""
    query = (query or "").strip()
    if not query:
        return []
    url = BING_URL.format(query=urllib.parse.quote(query))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", "ignore")
    except Exception as exc:
        log.warning("搜索失败 %r: %s", query, exc)
        return []
    results = parse_bing(page, limit=limit)
    for item in results[:fetch_top]:
        item.content = fetch_text(item.url)
    log.info("搜索 %r 命中 %d 条，抓取正文 %d 条", query, len(results),
             sum(1 for i in results if i.content))
    return results


def format_results(results: list[SearchResult]) -> str:
    """把搜索结果压成模型好读的纯文本。"""
    if not results:
        return "（没有搜到结果）"
    lines = []
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item.title}")
        if item.snippet:
            lines.append(f"   摘要: {item.snippet}")
        if item.content:
            lines.append(f"   正文: {item.content[:800]}")
        lines.append(f"   链接: {item.url}")
    return "\n".join(lines)


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索实时信息：新闻、天气、比分、股价、价格、最近发生的事等。"
            "凡是你不确定或可能过时的事实，都应该先调用它再回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量具体"},
            },
            "required": ["query"],
        },
    },
}

TOOLS = [WEB_SEARCH_TOOL]


def run_tool(name: str, arguments: dict) -> str:
    """执行模型请求的工具，返回给模型的文本结果。"""
    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        return format_results(web_search(query))
    return f"（未知工具: {name}）"