# -*- coding: utf-8 -*-
"""供应商层：embedding / rerank / llm 三个接口。

统一走 OpenAI 兼容协议，``base_url`` + ``model`` + ``api_key`` 决定一切，
换厂商只改 .env，不动代码。
"""

from __future__ import annotations

from .. import config
from .base import ApiError, CallBudget, chunked, post_json
from .openai_compat import EmbeddingClient, LLMClient, RerankClient

__all__ = [
    "ApiError",
    "CallBudget",
    "chunked",
    "post_json",
    "EmbeddingClient",
    "LLMClient",
    "RerankClient",
    "get_embedder",
    "get_llm",
    "get_reranker",
    "embedding_ready",
    "llm_ready",
]


def get_embedder(budget=None) -> EmbeddingClient:
    return EmbeddingClient(budget=budget)


def get_reranker(budget=None) -> RerankClient:
    return RerankClient(budget=budget)


def get_llm(budget=None) -> LLMClient:
    return LLMClient(budget=budget)


def embedding_ready() -> bool:
    return bool(config.EMBED_API_KEY)


def llm_ready() -> bool:
    return bool(config.LLM_API_KEY)
