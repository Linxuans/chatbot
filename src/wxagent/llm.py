# -*- coding: utf-8 -*-
"""DeepSeek / 任意 OpenAI 兼容接口的极简客户端。

不引入 openai SDK：只要 httpx，镜像小、依赖少、出错信息自己可控。
支持 function calling（工具调用），用于联网搜索。
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """模型调用失败（已重试过）。"""


class _Retryable(Exception):
    pass


def _extract_text(data: dict) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"接口返回结构异常: {str(data)[:300]}") from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if content:
        return str(content).strip()
    # 推理模型在思考阶段可能把正文放在 reasoning_content
    reasoning = message.get("reasoning_content")
    if reasoning:
        return str(reasoning).strip()
    return ""


class LLM:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._client = httpx.Client(
            timeout=httpx.Timeout(cfg.timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {cfg.api_key}" if cfg.api_key else "",
                "Content-Type": "application/json",
            },
            # 微信网关和核心可能在不同机器上，禁用环境代理更可控
            trust_env=False,
        )

    @property
    def endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # ---------------------------------------------------------------- 底层
    def _post(self, payload: dict, *, timeout: float | None, retries: int) -> dict:
        if not self.cfg.api_key:
            raise LLMError("DEEPSEEK_API_KEY 未配置")

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.post(
                    self.endpoint, json=payload, timeout=timeout or self.cfg.timeout
                )
                if resp.status_code in RETRYABLE_STATUS:
                    raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise LLMError(f"模型接口返回 {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            except (httpx.HTTPError, _Retryable) as exc:
                last_error = exc
                if attempt < retries:
                    delay = 1.5 * (attempt + 1)
                    log.warning("模型调用失败(%s)，%.1fs 后重试", exc, delay)
                    time.sleep(delay)
                    continue
                break
            except LLMError:
                raise

        raise LLMError(f"模型调用失败: {last_error}")

    def _payload(self, messages, temperature, max_tokens, tools) -> dict:
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # ---------------------------------------------------------------- 对外
    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        retries: int = 2,
    ) -> str:
        """只要最终文本。"""
        data = self._post(
            self._payload(messages, temperature, max_tokens, None),
            timeout=timeout,
            retries=retries,
        )
        text = _extract_text(data)
        if not text:
            raise LLMError("模型返回空内容")
        return text

    def chat_message(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        retries: int = 1,
    ) -> dict:
        """返回原始 assistant 消息（可能带 tool_calls）。"""
        data = self._post(
            self._payload(messages, temperature, max_tokens, tools),
            timeout=timeout,
            retries=retries,
        )
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"接口返回结构异常: {str(data)[:300]}") from exc

    def close(self) -> None:
        self._client.close()