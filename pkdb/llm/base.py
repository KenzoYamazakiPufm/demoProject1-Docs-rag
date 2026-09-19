# -*- coding: utf-8 -*-
"""供应商层基座：统一 HTTP 调用、429 指数退避、调用次数上限。

三条硬规则：
1. 统一走 OpenAI 兼容端点，换厂商只改配置
2. 429 / 5xx 指数退避重试（1/2/4/8 秒），不把免费额度浪费在失败请求上
3. 永不打印 API Key；错误信息给可操作提示
"""

from __future__ import annotations

import json
import time

import requests

from .. import config


class ApiError(RuntimeError):
    """调用外部 API 失败，携带可操作的排错提示。"""


class CallBudget:
    """单次运行的调用次数上限，防止误触发把免费额度刷爆。"""

    def __init__(self, limit: int = None):
        self.limit = config.MAX_API_CALLS_PER_RUN if limit is None else limit
        self.used = 0

    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.limit and self.used > self.limit:
            raise ApiError(
                "本次运行 API 调用次数已达上限 %d（已用 %d）。"
                "可调大 .env 中的 PKDB_MAX_API_CALLS，或分批建库。"
                % (self.limit, self.used)
            )

    def __repr__(self):
        return "CallBudget(used=%d, limit=%d)" % (self.used, self.limit)


def _hint(status: int, body: str) -> str:
    """把常见失败翻译成"下一句该干什么"。"""
    lowered = (body or "").lower()
    if status in (401, 403):
        return "鉴权失败：检查 .env 里的 API Key 是否正确、是否已实名认证。"
    if status == 402 or "30001" in body or "insufficient" in lowered:
        return (
            "账户余额不足，服务端拒绝调用。\n"
            "  ⚠️ 实测（2026-09）：硅基流动在账户没有可用余额时，"
            "**连官方标价 ¥0 的免费模型也一样返回 402**，绝不是只有付费模型才需要余额。\n"
            "  → 到控制台「费用中心」确认余额与代金券状态（代金券有有效期，过期即视为 0）。\n"
            "  → 若余额为零且不便充值，改走本地 embedding 即可（代码零改动），"
            "见 README「换厂商（不改代码）」。"
        )
    if status == 404:
        return "接口或模型不存在：检查 base_url 与模型 ID 是否匹配当前厂商。"
    if status == 429:
        return "触发限流（免费模型有固定限额）：稍后重试，或换用付费模型 ID。"
    if status >= 500:
        return "服务端错误：稍后重试；若持续失败请检查 base_url。"
    if "model" in lowered:
        return "提示：模型 ID 可能不受支持，检查 .env 中的 PKDB_*_MODEL。"
    return "请检查 .env 配置（base_url / model / api_key）与网络连通性。"


def post_json(url: str, payload: dict, api_key: str, timeout: int = 60,
              retries: int = None, budget: CallBudget = None):
    """统一 POST。仅对 429 / 5xx 做指数退避重试。"""
    attempts = config.RETRY_MAX if retries is None else retries
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
    }
    last_error = None

    for attempt in range(attempts + 1):
        if budget is not None:
            budget.spend()
        try:
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 timeout=timeout)
        except requests.RequestException as exc:
            last_error = "网络异常：%s" % exc
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF[min(attempt, len(config.RETRY_BACKOFF) - 1)])
                continue
            raise ApiError("%s（已重试 %d 次）" % (last_error, attempts)) from exc

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError("返回内容不是合法 JSON：%s" % resp.text[:200]) from exc

        body = resp.text[:300]
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
            time.sleep(config.RETRY_BACKOFF[min(attempt, len(config.RETRY_BACKOFF) - 1)])
            continue

        raise ApiError(
            "HTTP %d 调用失败：%s\n  %s" % (resp.status_code, body, _hint(resp.status_code, body))
        )

    raise ApiError("调用失败：%s" % last_error)


def chunked(items, size: int):
    """按固定大小切批（批量请求，减少调用次数）。"""
    for start in range(0, len(items), size):
        yield items[start:start + size]
