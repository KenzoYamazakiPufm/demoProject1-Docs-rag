# -*- coding: utf-8 -*-
"""OpenAI 兼容端点实现（embedding / rerank / chat）。

默认可用的免费组合：
    embedding + rerank -> 硅基流动  https://api.siliconflow.cn/v1
    chat              -> 智谱      https://open.bigmodel.cn/api/paas/v4

也可整体切到 DeepSeek（chat）：https://api.deepseek.com + deepseek-flash
"""

from __future__ import annotations

from .. import config
from .base import ApiError, CallBudget, chunked, post_json


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


class EmbeddingClient:
    """把文字批量转成向量。命中缓存由调用方负责，本类只负责"发出去、收回来"。"""

    def __init__(self, base_url: str = None, model: str = None, api_key: str = None,
                 budget: CallBudget = None):
        self.base_url = base_url or config.EMBED_BASE_URL
        self.model = model or config.EMBED_MODEL
        self.api_key = api_key or config.EMBED_API_KEY
        self.budget = budget

    def embed(self, texts):
        """输入 ``list[str]``，返回 ``list[list[float]]``，顺序与输入一致。"""
        items = list(texts)
        if not items:
            return []
        if not self.api_key:
            raise ApiError(
                "未配置 embedding API Key。请在 .env 中设置 PKDB_EMBED_API_KEY"
                "（硅基流动的 Key），或先用 --no-embed 建库后再补向量。"
            )

        vectors = []
        for batch in chunked(items, config.EMBED_BATCH):
            payload = {
                "model": self.model,
                "input": batch,
                "encoding_format": "float",
            }
            data = post_json(_url(self.base_url, "/embeddings"), payload,
                             self.api_key, timeout=config.LLM_TIMEOUT,
                             budget=self.budget)
            rows = data.get("data") or []
            if len(rows) != len(batch):
                raise ApiError(
                    "embedding 返回条数与请求不一致：请求 %d 条，返回 %d 条"
                    % (len(batch), len(rows))
                )
            # 有 index 字段时按它排序，避免服务端乱序
            if rows and isinstance(rows[0], dict) and "index" in rows[0]:
                rows = sorted(rows, key=lambda r: r["index"])
            vectors.extend(row["embedding"] for row in rows)
        return vectors


class RerankClient:
    """对 (query, document) 成对精排。可选功能，失败时由调用方降级为不重排。"""

    def __init__(self, base_url: str = None, model: str = None, api_key: str = None,
                 budget: CallBudget = None):
        self.base_url = base_url or config.RERANK_BASE_URL
        self.model = model or config.RERANK_MODEL
        self.api_key = api_key or config.RERANK_API_KEY
        self.budget = budget

    @property
    def enabled(self) -> bool:
        return bool(config.RERANK_ENABLED and self.api_key)

    def rerank(self, query: str, documents, top_n: int = None):
        """返回 ``[(原始下标, 相关度分数)]``，按分数降序。"""
        docs = list(documents)
        if not docs or not self.api_key:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "top_n": top_n or len(docs),
            "return_documents": False,
        }
        data = post_json(_url(self.base_url, "/rerank"), payload,
                         self.api_key, timeout=config.LLM_TIMEOUT,
                         budget=self.budget)
        results = data.get("results") or []
        pairs = [(int(r.get("index", i)), float(r.get("relevance_score", 0.0)))
                 for i, r in enumerate(results)]
        pairs.sort(key=lambda item: item[1], reverse=True)
        return pairs


class LLMClient:
    """对话生成：把"指令 + 召回原文 + 问题"组织成人话。"""

    def __init__(self, base_url: str = None, model: str = None, api_key: str = None,
                 budget: CallBudget = None):
        self.base_url = base_url or config.LLM_BASE_URL
        self.model = model or config.LLM_MODEL
        self.api_key = api_key or config.LLM_API_KEY
        self.budget = budget

    def chat(self, system: str, user: str, temperature: float = None,
             max_tokens: int = None) -> str:
        if not self.api_key:
            raise ApiError(
                "未配置对话模型 API Key。请在 .env 中设置 PKDB_LLM_API_KEY"
                "（智谱或 DeepSeek 的 Key）。"
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": config.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
            "stream": False,
        }
        # 厂商特性开关（最典型：DeepSeek 默认开思考模式，用
        # {"thinking": {"type": "disabled"}} 关掉）。
        # messages 是提示词的载体、由本模块负责，不允许被配置覆盖，
        # 否则用户填错一个键就会把整个提示词顶掉。
        extra = dict(config.LLM_EXTRA_BODY)
        extra.pop("messages", None)
        payload.update(extra)
        data = post_json(_url(self.base_url, "/chat/completions"), payload,
                         self.api_key, timeout=config.LLM_TIMEOUT,
                         budget=self.budget)
        choices = data.get("choices") or []
        if not choices:
            raise ApiError("对话接口返回为空：%s" % str(data)[:200])
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()
