# -*- coding: utf-8 -*-
"""回答层校验：引用必附来源、无召回不生成、提示词不含未召回内容。"""

from __future__ import annotations

from tests.helpers import seed_chunks

from pkdb import answer as answer_mod
from pkdb import config, db, retrieve


class FakeLLM:
    """假对话模型：可以正常回复，也可以模拟失败。"""

    api_key = "fake-key"

    def __init__(self, reply="\u7b54\u6848[1]", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


def _seed(conn):
    seed_chunks(conn, [
        {"chunk_id": "c_auto", "doc_id": "d1", "section": config.SECTION_WORK,
         "heading": "A公司", "para_start": 10, "para_end": 16,
         "text": "负责自动化测试框架搭建，使用 Selenium 与 Pytest 编写接口用例。"},
        # 这段刻意与检索词完全不共享字符，用来验证"未召回内容不进提示词"
        {"chunk_id": "c_host", "doc_id": "d1", "section": config.SECTION_STRENGTH,
         "heading": "核心优势", "para_start": 3, "para_end": 6,
         "text": "担任大型晚会司仪，负责全场流程编排与嘉宾接待。"},
    ])


def _ask(conn, question, llm=None):
    return answer_mod.ask(
        conn, question, mode="keyword", top_k=3, use_rerank=False, llm=llm
    )


def test_no_recall_returns_fixed_text_without_calling_llm(conn):
    _seed(conn)
    llm = FakeLLM()
    # 用一份与简历毫无交集的词汇，确保任何一路都召不回
    result = _ask(conn, "量子纠缠实验数据", llm=llm)
    assert result.text == answer_mod.NO_MATCH_TEXT
    assert result.citations == []
    assert result.hits == []
    assert llm.calls == [], "无召回时不得调用大模型"


def test_answer_always_carries_citations(conn):
    _seed(conn)
    result = _ask(conn, "自动化测试框架", llm=FakeLLM())
    assert result.hits
    assert len(result.citations) == len(result.hits)
    for cite in result.citations:
        assert cite.file_name
        assert cite.section
        assert cite.para_range


def test_degraded_output_when_no_llm(conn):
    _seed(conn)
    result = _ask(conn, "自动化测试框架", llm=None)
    assert result.degraded is True
    assert result.citations, "降级输出也必须带来源"
    assert "自动化测试" in result.text


def test_llm_failure_degrades_instead_of_raising(conn):
    _seed(conn)
    llm = FakeLLM(error=RuntimeError("429 too many requests"))
    result = _ask(conn, "自动化测试框架", llm=llm)
    assert result.degraded is True
    assert result.citations
    assert result.error and "429" in result.error


def test_llm_success_marks_not_degraded(conn):
    _seed(conn)
    llm = FakeLLM(reply="\u4ed6\u5728 A \u516c\u53f8\u505a\u8fc7\u81ea\u52a8\u5316\u6d4b\u8bd5\u3002[1]")
    result = _ask(conn, "自动化测试框架", llm=llm)
    assert result.degraded is False
    assert result.text.startswith("\u4ed6\u5728")
    assert len(llm.calls) == 1


def test_prompt_contains_only_retrieved_chunks(conn):
    """未召回的片段绝不能进提示词。"""
    _seed(conn)
    hits = retrieve.search(
        conn, "自动化测试框架", mode="keyword", top_k=3, use_rerank=False
    )
    assert hits, "应当至少召回一段"
    citations = answer_mod._citations(conn, hits)
    prompt = answer_mod.build_user_prompt(conn, "自动化测试框架", hits, citations)

    assert hits[0].chunk_id == "c_auto"
    for hit in hits:
        assert hit.text.strip() in prompt, "召回的片段必须原样出现在提示词里"
    assert "自动化测试框架" in prompt, "用户问题必须出现在提示词里"

    # 库里所有"没被召回"的块，一个字都不许进提示词
    hit_ids = {hit.chunk_id for hit in hits}
    for row in db.list_chunks(conn, only_leaf=True):
        if row["chunk_id"] not in hit_ids:
            assert row["text"] not in prompt
    assert "司仪" not in prompt


def test_system_prompt_forbids_fabrication():
    assert "禁止" in answer_mod.SYSTEM_PROMPT
    assert "简历里没有写到这部分" in answer_mod.SYSTEM_PROMPT


def test_no_answer_detection():
    """区分"模型拒答"与"本地没召回"——这正是用户看到"未找到 + 命中3段"并排时的困惑源。"""
    assert answer_mod.looks_like_no_answer("未找到相关内容。")
    # 新话术：模型被要求使用的标准拒答说法
    assert answer_mod.looks_like_no_answer("简历里没有写到这部分。")
    # 旧话术兜底：7B 模型未必严格照做，旧标记必须继续有效
    assert answer_mod.looks_like_no_answer("参考片段中没有相关信息。片段里写的是……")
    assert not answer_mod.looks_like_no_answer("他在泰雷兹搭建并维护了 CI 环境[1]。")
    assert not answer_mod.looks_like_no_answer("")


def test_answer_flags_no_answer_without_losing_hits(conn):
    """模型拒答时，仍然要保留命中的片段与来源，方便用户自己核对。"""
    _seed(conn)
    llm = FakeLLM(reply="简历里没有写到这部分。")
    result = _ask(conn, "自动化测试框架", llm=llm)

    assert result.no_answer is True
    assert result.degraded is False
    assert result.hits and result.citations, "拒答也不能把来源丢掉"
    assert result.text != answer_mod.NO_MATCH_TEXT, "这不是『没召回』，两者必须可区分"


def test_system_prompt_has_no_internal_labels():
    """防的是线上真实出现的一幕：问「微软做了哪些工作？」，回答开头是
    「根据提供的《参考片段》，微软相关的项目包括：」——
    模型照抄了提示词里的材料标题，把内部术语漏到了用户眼前。

    「参考片段」这类书名号标签是写给模型看的内部结构，不得再出现在提示词里。
    """
    prompt = answer_mod.SYSTEM_PROMPT
    assert "参考片段" not in prompt, "内部标签不得出现在提示词里"
    assert "《结构化任职记录》" not in prompt, "另一个内部标签同样不得出现"
    assert "简历里没有写到这部分" in prompt, "拒答话术必须是用户指定的那一句"


def test_user_prompt_has_no_internal_labels(conn):
    """同上，但盯的是材料标题本体 —— 模型是照抄它才会说出《参考片段》。"""
    _seed(conn)
    hits = retrieve.search(
        conn, "自动化测试框架", mode="keyword", top_k=3, use_rerank=False
    )
    assert hits
    citations = answer_mod._citations(conn, hits)
    prompt = answer_mod.build_user_prompt(conn, "自动化测试框架", hits, citations)

    assert "参考片段" not in prompt, "材料标题必须自然化，否则模型仍会照抄"
    assert "他的简历" in prompt, "应当改用自然说法指代材料"
