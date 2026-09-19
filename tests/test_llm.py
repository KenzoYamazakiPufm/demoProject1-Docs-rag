# -*- coding: utf-8 -*-
"""对话请求体组装：厂商特性开关既要**真的生效**，又要**不越权**。

背景：DeepSeek 官方文档写明「思考模式默认打开，且 effort 默认为 high」，
必须靠 `{"thinking": {"type": "disabled"}}` 才能关掉。我们把这个开关做成
`PKDB_LLM_EXTRA_BODY` 原样透传。这种"透传式配置"最容易出两类事故：

1. 用户填了，但字段没进请求体 —— 表现为"以为关了思考模式，其实没关"，
   又慢又贵，而且**不报错**，是最难排查的一种。
2. 用户填错键名，把 `messages` 顶掉 —— 提示词整段失效，大模型开始自由发挥。

两条都在下面钉死（全程不联网，post_json 被替换成截获器）。
"""

from __future__ import annotations

from pkdb import config
from pkdb.llm import openai_compat


class _Capture:
    """替掉 post_json：把真正发出去的请求体截下来，不发网络请求。"""

    def __init__(self):
        self.url = None
        self.payload = None

    def __call__(self, url, payload, api_key, **kwargs):
        self.url = url
        self.payload = payload
        return {"choices": [{"message": {"content": "OK"}}]}


def _chat(monkeypatch, extra):
    cap = _Capture()
    monkeypatch.setattr(openai_compat, "post_json", cap)
    monkeypatch.setattr(config, "LLM_EXTRA_BODY", extra, raising=False)

    client = openai_compat.LLMClient(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_key="fake-key",
    )
    reply = client.chat("系统指令", "用户问题")
    return cap, reply


def test_extra_body_lands_in_request_payload(monkeypatch):
    """填了就必须真的进请求体 —— 否则"以为关了其实没关"。"""
    cap, _ = _chat(monkeypatch, {"thinking": {"type": "disabled"}})

    assert cap.payload["thinking"] == {"type": "disabled"}
    # base_url 末尾没有 /v1 时也要能拼对路径
    assert cap.url == "https://api.deepseek.com/chat/completions"


def test_empty_extra_body_changes_nothing(monkeypatch):
    """不填时请求体保持原样，不给别家厂商塞多余字段。"""
    cap, _ = _chat(monkeypatch, {})

    assert "thinking" not in cap.payload
    assert set(cap.payload) == {
        "model", "messages", "temperature", "max_tokens", "stream",
    }


def test_extra_body_cannot_override_messages(monkeypatch):
    """messages 由本模块负责：用户填错键也不许把提示词顶掉。"""
    cap, _ = _chat(monkeypatch, {"messages": [{"role": "user", "content": "伪造"}]})

    assert cap.payload["messages"][0]["content"] == "系统指令"
    assert cap.payload["messages"][1]["content"] == "用户问题"


def test_extra_body_can_override_sampling_params(monkeypatch):
    """其余键允许覆盖，作为逃生门（如临时调 temperature / max_tokens）。"""
    cap, _ = _chat(monkeypatch, {"temperature": 0.9, "max_tokens": 2048})

    assert cap.payload["temperature"] == 0.9
    assert cap.payload["max_tokens"] == 2048


def test_chat_returns_assistant_content(monkeypatch):
    """基本契约：取 choices[0].message.content 并去掉首尾空白。"""
    cap = _Capture()
    monkeypatch.setattr(openai_compat, "post_json", cap)
    monkeypatch.setattr(config, "LLM_EXTRA_BODY", {}, raising=False)

    client = openai_compat.LLMClient(api_key="fake-key")
    assert client.chat("s", "u") == "OK"


def test_chat_requires_api_key(monkeypatch):
    """没有 Key 时给出可操作的报错，而不是发出一个必然 401 的请求。

    注意：`LLMClient` 用 `api_key or config.LLM_API_KEY` 兜底，所以只传空串
    是**没用的**（会回落到 .env 里的真实 Key），必须把配置也清掉。
    """
    monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
    client = openai_compat.LLMClient(base_url="https://x", model="m", api_key="")

    try:
        client.chat("s", "u")
    except openai_compat.ApiError as exc:
        assert "PKDB_LLM_API_KEY" in str(exc)
    else:  # pragma: no cover - 走到这里说明契约被破坏
        raise AssertionError("未配置 Key 时必须抛 ApiError")
