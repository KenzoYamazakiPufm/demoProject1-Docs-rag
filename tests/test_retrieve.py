# -*- coding: utf-8 -*-
"""检索层校验（全程离线，用预置假向量）。"""

from __future__ import annotations

import numpy as np

from tests.helpers import FakeEmbedder, seed_chunks, store_fake_vector

from pkdb import config, retrieve

DIM = config.EMBED_DIM


def vec(*values):
    """构造一个与真实模型同维度的稀疏向量，方便手工指定相似度。"""
    arr = np.zeros(DIM, dtype="<f4")
    for index, value in enumerate(values):
        arr[index] = value
    return arr.tolist()


# ------------------------------------------------------------- 分词与 BM25

def test_tokenize_filters_punctuation():
    tokens = retrieve.tokenize("自动化测试，Selenium / Pytest。")
    assert tokens
    assert all(token.strip() for token in tokens)
    assert not any(token in "，。/ " for token in tokens)


def test_bm25_ranks_relevant_first():
    ids = ["A", "B", "C"]
    texts = [
        "负责自动化测试框架搭建，使用 Selenium 与 Pytest。",
        "负责公司年会主持与活动策划。",
        "参与需求评审与测试用例设计。",
    ]
    ranking = retrieve.bm25_rank(ids, texts, "自动化测试框架")
    assert ranking, "BM25 应当至少命中一段"
    assert ranking[0][0] == "A"


def test_bm25_empty_query_returns_empty():
    assert retrieve.bm25_rank(["A"], ["内容"], "") == []


def test_bm25_works_on_tiny_corpus():
    """回归用例：``BM25Okapi`` 在 2 篇文档时 IDF 恰好为 0，会让关键词路整体失效。

    本项目只有 30 多个子块，属于典型小语料，所以必须锁住"小语料也能召回"。
    """
    ids = ["A", "B"]
    texts = [
        "Automation testing experience with Selenium",
        "负责公司年会主持与活动策划",
    ]
    ranking = retrieve.bm25_rank(ids, texts, "automation testing")
    assert ranking, "小语料下 BM25 也必须能召回"
    assert [cid for cid, _ in ranking] == ["A"], "无关文档不得因基线分被召回"


# --------------------------------------------------------------- 向量路

def test_vector_rank_orders_by_cosine():
    ids = ["A", "B"]
    matrix = np.array([vec(1.0, 0.0), vec(0.0, 1.0)], dtype="<f4")
    ranking = retrieve.vector_rank(ids, matrix, vec(1.0, 0.0))
    assert [cid for cid, _ in ranking] == ["A", "B"]


def test_vector_rank_dimension_mismatch_is_safe():
    ids = ["A"]
    matrix = np.array([vec(1.0, 0.0)], dtype="<f4")
    assert retrieve.vector_rank(ids, matrix, [1.0, 0.0, 0.0]) == []


# ------------------------------------------------------------------ RRF

def test_rrf_rewards_mutual_agreement():
    """A 两路都在前排 → 融合后应当第一；单路第一的 C 反而不该赢。"""
    bm25 = [("A", 9.9), ("B", 5.0), ("C", 1.0)]
    vector = [("C", 0.99), ("A", 0.90), ("B", 0.80)]
    fused = retrieve.rrf_fuse([bm25, vector], k=60)
    order = [chunk_id for chunk_id, _ in fused]
    assert order == ["A", "C", "B"]


def test_rrf_respects_top_n():
    fused = retrieve.rrf_fuse([[("A", 1), ("B", 1), ("C", 1)]], k=60, top_n=2)
    assert len(fused) == 2


# -------------------------------------------------------------- search 入口

def _seed(conn):
    seed_chunks(conn, [
        {"chunk_id": "c_auto", "doc_id": "d1", "section": config.SECTION_WORK,
         "heading": "A公司", "para_start": 0, "para_end": 5,
         "text": "负责自动化测试框架搭建，使用 Selenium 与 Pytest 编写接口自动化用例。"},
        {"chunk_id": "c_party", "doc_id": "d1", "section": config.SECTION_STRENGTH,
         "heading": "核心优势", "para_start": 5, "para_end": 8,
         "text": "负责公司年会主持与团队活动策划，擅长跨部门沟通。"},
        {"chunk_id": "c_project", "doc_id": "d1", "section": config.SECTION_PROJECT,
         "heading": "银行交易数据监管审核系统", "para_start": 8, "para_end": 12,
         "text": "主导银行交易数据监管审核系统测试，覆盖十年日志存储与性能压测。"},
    ])
    store_fake_vector(conn, "c_auto", vec(1.0, 0.0))
    store_fake_vector(conn, "c_party", vec(0.0, 1.0))
    store_fake_vector(conn, "c_project", vec(0.5, 0.5))


def test_search_keyword_mode_needs_no_embedder(conn):
    _seed(conn)
    hits = retrieve.search(conn, "自动化测试框架", mode="keyword", use_rerank=False)
    assert hits
    assert hits[0].chunk_id == "c_auto"
    assert hits[0].source == "bm25"


def test_search_hybrid_merges_two_routes(conn):
    _seed(conn)
    embedder = FakeEmbedder({"自动化测试": vec(1.0, 0.0)})
    hits = retrieve.search(conn, "自动化测试", mode="hybrid", use_rerank=False,
                           embedder=embedder)
    assert hits
    assert hits[0].source == "rrf"
    assert hits[0].chunk_id == "c_auto"


def test_search_structured_filter_narrows_candidates(conn):
    _seed(conn)
    hits = retrieve.search(conn, "测试", mode="keyword", use_rerank=False,
                           section=config.SECTION_PROJECT)
    assert hits
    assert {hit.chunk_id for hit in hits} == {"c_project"}


def test_search_language_filter(conn):
    seed_chunks(conn, [
        {"chunk_id": "zh1", "doc_id": "dz", "lang": "zh", "text": "自动化测试经验丰富",
         "para_start": 0, "para_end": 1},
        {"chunk_id": "en1", "doc_id": "de", "lang": "en",
         "text": "Automation testing experience", "para_start": 0, "para_end": 1},
    ])
    hits_en = retrieve.search(conn, "automation testing", mode="keyword",
                              use_rerank=False, lang="en")
    assert {hit.chunk_id for hit in hits_en} == {"en1"}

    hits_zh = retrieve.search(conn, "自动化测试", mode="keyword",
                              use_rerank=False, lang="zh")
    assert {hit.chunk_id for hit in hits_zh} == {"zh1"}


def test_search_without_vectors_still_works(conn):
    """库里没有向量时，语义路自动缺席，关键词路仍然可用。"""
    _seed(conn)
    embedder = FakeEmbedder({"测试": vec(1.0, 0.0)})
    hits = retrieve.search(conn, "银行交易数据", mode="hybrid", use_rerank=False,
                           embedder=embedder)
    assert hits
    assert hits[0].chunk_id == "c_project"


def test_search_blank_query_returns_empty(conn):
    _seed(conn)
    assert retrieve.search(conn, "   ", mode="keyword") == []


def test_filter_entries_by_structured_fields(conn):
    seed_chunks(conn, [
        {"chunk_id": "e1", "doc_id": "d2", "section": config.SECTION_WORK,
         "heading": "A公司", "para_start": 0, "para_end": 3, "text": "自动化测试"},
    ])
    conn.execute(
        "INSERT INTO profile_entries (entry_id, kind, title, role, start_ym, end_ym,"
        " tech, chunk_id, raw) VALUES ('x1','company','A公司','测试工程师',"
        "'2016-10','2022-12','Selenium,Pytest','e1','')"
    )
    conn.commit()

    assert retrieve.filter_entries(conn, kind="company")
    assert retrieve.filter_entries(conn, kind="company", tech_like="Selenium")
    # 起止时间语义：start_ym >= start_from
    assert retrieve.filter_entries(conn, kind="company", start_from="2016-01")
    assert retrieve.filter_entries(conn, kind="company", start_to="2016-12")
    assert not retrieve.filter_entries(conn, kind="company", start_from="2017-01")
    assert not retrieve.filter_entries(conn, kind="company", title_like="不存在的公司")
