# -*- coding: utf-8 -*-
"""年份路：让"2017 在哪个公司"这类问题能被精确召回。

这是对一次**真实故障**的回归保护。故障现象：问「2017在那个公司」，
系统回「参考片段中没有相关信息」，而检索却显示"命中 12 段"。

根因不在模型 —— 简历只用区间写法（``2016.10 – 2022.12``），正文里
**从不出现"2017"这三个字**，于是：

    关键词路（BM25）：字面不存在 → 全灭
    向量路：把 2017 当成与 **2016 最像的数字** → 召回的全是 2016 年前后的块

模型拿到的是"博彦科技（2013.10–2016.10）"这类片段，只能诚实地说"片段里没有"。
而"某年是否落在某个区间内"是**逻辑运算**，语义相似度天生做不了这件事。

所以补了「块 → 年份」派生索引这一路。注意它**不碰任何向量**：
索引完全由 ``profile_entries`` 推导，不改 chunk 文本，重建零 API 成本。
"""

from __future__ import annotations

from tests.helpers import seed_chunks

from pkdb import answer, config, db, retrieve


def _seed_two_companies(conn):
    """造两个单位：博彦（2013-10 ~ 2016-10）与泰雷兹（2016-10 ~ 2022-12）。

    2017 **只落在泰雷兹里** —— 这就是真实简历的结构。
    """
    seed_chunks(conn, [
        {"chunk_id": "c_beyond", "doc_id": "d1", "section": config.SECTION_WORK,
         "heading": "博彦科技（Beyondsoft）| 2013.10 – 2016.10",
         "para_start": 20, "para_end": 27,
         "text": "驻场服务汤森路透，主导质量保障与自动化测试代码开发。"},
        {"chunk_id": "c_thales", "doc_id": "d1", "section": config.SECTION_WORK,
         "heading": "泰雷兹（Thales）| 2016.10 – 2022.12",
         "para_start": 27, "para_end": 40,
         "text": "系统测试工程师，负责物联网蜂窝通信模块测试与 CI 环境建设。"},
    ])
    db.replace_profile_entries(conn, "d1", [
        {"entry_id": "e1", "kind": "company", "title": "博彦科技（Beyondsoft）",
         "role": "SDET", "start_ym": "2013-10", "end_ym": "2016-10",
         "tech": None, "chunk_id": "c_beyond", "raw": ""},
        {"entry_id": "e2", "kind": "company", "title": "泰雷兹（Thales）",
         "role": "系统测试工程师", "start_ym": "2016-10", "end_ym": "2022-12",
         "tech": None, "chunk_id": "c_thales", "raw": ""},
    ])
    conn.commit()
    db.rebuild_chunk_years(conn)


def _seed_bilingual(conn):
    """同一单位在 CN / EN 文档里各存一条（真实简历就是这样）。"""
    seed_chunks(conn, [
        {"chunk_id": "c_zh", "doc_id": "d_zh", "lang": "zh",
         "section": config.SECTION_WORK, "para_start": 0, "para_end": 5,
         "heading": "泰雷兹（Thales）| 2016.10 – 2022.12", "text": "负责系统测试。"},
        {"chunk_id": "c_en", "doc_id": "d_en", "lang": "en",
         "section": config.SECTION_WORK, "para_start": 0, "para_end": 5,
         "heading": "Thales | Oct 2016 - Dec 2022", "text": "System testing."},
    ])
    db.replace_profile_entries(conn, "d_zh", [
        {"entry_id": "e_zh", "kind": "company", "title": "泰雷兹（Thales）",
         "role": "系统测试工程师", "start_ym": "2016-10", "end_ym": "2022-12",
         "tech": None, "chunk_id": "c_zh", "raw": ""},
    ])
    db.replace_profile_entries(conn, "d_en", [
        {"entry_id": "e_en", "kind": "company", "title": "Thales",
         "role": "System Test Engineer", "start_ym": "2016-10", "end_ym": "2022-12",
         "tech": None, "chunk_id": "c_en", "raw": ""},
    ])
    conn.commit()
    db.rebuild_chunk_years(conn)


# --------------------------------------------------------------- 年月展开

def test_years_of_period_expands_inclusive_range():
    assert db.years_of_period("2016-10", "2022-12") == [
        2016, 2017, 2018, 2019, 2020, 2021, 2022,
    ]
    assert db.years_of_period("2016", "2016") == [2016]


def test_years_of_period_handles_open_ended():
    """终点是"至今 / present"时展开到给定年份（默认今年）。"""
    assert db.years_of_period("2023-01", "\u81f3\u4eca", now_year=2026) == [
        2023, 2024, 2025, 2026,
    ]
    assert db.years_of_period("2024-05", "present", now_year=2026) == [2024, 2025, 2026]


def test_years_of_period_refuses_to_guess():
    """宁可这条记录不参与年份召回，也不要瞎猜一个年份。"""
    assert db.years_of_period(None, "2022-12") == []          # 没有起点
    assert db.years_of_period("2016-10", None) == []          # 没有终点
    assert db.years_of_period("2016-10", "\u4e71\u7801") == []  # 终点无法识别
    assert db.years_of_period("2022-12", "2016-10") == []     # 终点早于起点
    assert db.years_of_period("1900-01", "2020-01") == []     # 跨度异常大


# --------------------------------------------------------------- 问句抽年份

def test_years_in_query_extracts_and_dedups():
    assert retrieve.years_in_query("2017在那个公司") == [2017]
    assert retrieve.years_in_query("2018 到 2017 之间") == [2017, 2018]
    assert retrieve.years_in_query("他有几年工作经验") == []


def test_years_in_query_respects_digit_boundaries():
    """不能把长数字里的片段当成年份。"""
    assert retrieve.years_in_query("工单号 20165 的处理情况") == []
    assert retrieve.years_in_query("编号12017") == []


# --------------------------------------------------------------- 派生索引

def test_index_is_derived_from_profile_entries(conn):
    _seed_two_companies(conn)
    by_chunk = {}
    for row in conn.execute("SELECT chunk_id, year FROM chunk_years"):
        by_chunk.setdefault(row["chunk_id"], []).append(row["year"])

    assert sorted(by_chunk["c_thales"]) == [2016, 2017, 2018, 2019, 2020, 2021, 2022]
    assert sorted(by_chunk["c_beyond"]) == [2013, 2014, 2015, 2016]
    # 2017 只能属于泰雷兹 —— 这正是关键词路永远找不到的那条信息
    assert 2017 not in by_chunk["c_beyond"]


def test_index_rebuild_is_idempotent(conn):
    _seed_two_companies(conn)
    first = conn.execute("SELECT COUNT(*) AS c FROM chunk_years").fetchone()["c"]
    db.rebuild_chunk_years(conn)
    second = conn.execute("SELECT COUNT(*) AS c FROM chunk_years").fetchone()["c"]
    assert first == second, "重建派生索引不该产生重复行"


def test_index_skips_entries_without_a_real_chunk(conn):
    """结构化记录指向不存在的块时，不得在索引里留下查不到的幽灵。"""
    seed_chunks(conn, [{"chunk_id": "c_real", "doc_id": "d1",
                        "para_start": 0, "para_end": 5,
                        "text": "正文内容" * 5}])
    db.replace_profile_entries(conn, "d1", [
        {"entry_id": "e1", "kind": "company", "title": "有块的",
         "role": None, "start_ym": "2016-01", "end_ym": "2017-12",
         "tech": None, "chunk_id": "c_real", "raw": ""},
        {"entry_id": "e2", "kind": "company", "title": "没有对应块的",
         "role": None, "start_ym": "2016-01", "end_ym": "2017-12",
         "tech": None, "chunk_id": "c_ghost", "raw": ""},
    ])
    conn.commit()
    db.rebuild_chunk_years(conn)

    ids = {row["chunk_id"] for row in conn.execute("SELECT chunk_id FROM chunk_years")}
    assert ids == {"c_real"}


def test_year_rank_respects_prefilter(conn):
    """年份路必须守 section / lang 预过滤的边界，不能绕过去。"""
    _seed_two_companies(conn)
    assert retrieve.year_rank(conn, [2017], ["c_beyond"]) == []
    assert [cid for cid, _ in retrieve.year_rank(conn, [2017], ["c_thales"])] == ["c_thales"]
    assert retrieve.year_rank(conn, [], ["c_thales"]) == []


# --------------------------------------------------- 权重（这一段才是重点）

def test_year_channel_must_outweigh_a_wrong_consensus():
    """年份路必须能**单挑**另外两路的合谋 —— 这是权重存在的唯一理由。

    真实情形正是如此：泰雷兹那段在问句上"一个字都不沾"（简历里根本没有"2017"），
    所以它**不会出现在 BM25 / 向量榜单里**；而错误的那段会同时占据两路第 1 名。
    不加权就是 1 票对 2 票，必输。
    """
    wrong = [("c_wrong", 9.0)]
    year = [("c_right", 1.0)]

    unweighted = retrieve.rrf_fuse([wrong, wrong, year], k=60)
    assert unweighted[0][0] == "c_wrong", "无权重时确实会输 —— 正是要加权的理由"

    weighted = retrieve.rrf_fuse(
        [wrong, wrong, year], k=60,
        weights=[1.0, 1.0, config.YEAR_CHANNEL_WEIGHT],
    )
    assert weighted[0][0] == "c_right", "加权后精确命中必须翻盘"


def test_rrf_weight_count_mismatch_falls_back_to_equal():
    """权重个数对不上时退回全 1，而不是错配着算出一份没人看得懂的排名。"""
    rankings = [[("a", 1.0)], [("b", 1.0)]]
    assert retrieve.rrf_fuse(rankings, k=60, weights=[5.0]) == \
        retrieve.rrf_fuse(rankings, k=60)


# ------------------------------------------------------- 端到端（故障复现）

def test_query_with_year_recalls_the_right_company(conn):
    """回归用例：问"2017 在哪个公司"必须召回泰雷兹。

    前提先锁死：在这个库上**关键词路是真的一个字都找不到**。所以这条用例
    能通过，只可能来自年份路。
    """
    _seed_two_companies(conn)

    rows = db.list_chunks(conn, only_leaf=True)
    empty = retrieve.bm25_rank([r["chunk_id"] for r in rows],
                               [r["text"] for r in rows], "2017在那个公司")
    assert empty == [], "前提：没有任何一段字面含 2017（否则这条用例说明不了问题）"

    hits = retrieve.search(conn, "2017在那个公司", mode="keyword",
                           top_k=5, use_rerank=False)
    assert hits, "年份路必须把内容召回来"
    assert hits[0].chunk_id == "c_thales", "2017 年应当在泰雷兹"
    assert hits[0].source == "year", "只有年份路有票时，来源应标为 year"
    assert "c_beyond" not in [h.chunk_id for h in hits], "不覆盖 2017 的单位不得被召回"


def test_query_without_year_is_unchanged(conn):
    """没有年份的问句，年份路必须完全静默（不引入任何回归）。"""
    _seed_two_companies(conn)
    hits = retrieve.search(conn, "自动化测试代码开发", mode="keyword",
                           top_k=5, use_rerank=False)
    assert hits, "正常的字面检索仍要工作"
    assert hits[0].chunk_id == "c_beyond"
    assert all(hit.source != "year" for hit in hits)


# ------------------------------- 结构化证据（真正让问题"答得出来"的一环）

def test_structured_evidence_computes_year_to_company(conn):
    """"2017 属于哪家"必须由代码算死，不能交给模型去推区间。"""
    _seed_two_companies(conn)
    text = answer.structured_evidence(conn, "2017在那个公司")

    assert text, "有年份就应当给出结构化证据"
    assert "泰雷兹" in text
    assert "2016-10 ~ 2022-12" in text
    assert "博彦" not in text, "不覆盖 2017 的单位不得出现"


def test_structured_evidence_is_silent_without_year(conn):
    """没有年份时行为与改动前完全一致（不引入任何回归）。"""
    _seed_two_companies(conn)
    assert answer.structured_evidence(conn, "他做过自动化测试吗") == ""


def test_structured_evidence_refuses_when_nothing_covers(conn):
    """年份落在所有记录之外时，宁可不给，也不要给错的。"""
    _seed_two_companies(conn)
    assert answer.structured_evidence(conn, "1998年他在哪家公司") == ""


def test_structured_evidence_dedups_by_question_language(conn):
    """同一单位在 CN / EN 各存一条，问中文时不该两种语言都报一遍。"""
    _seed_bilingual(conn)

    zh = answer.structured_evidence(conn, "2017在哪个公司")
    assert "系统测试工程师" in zh
    assert "System Test Engineer" not in zh

    en = answer.structured_evidence(conn, "Which company in 2017?")
    assert "System Test Engineer" in en
    assert "系统测试工程师" not in en


def test_prompt_carries_structured_evidence_and_says_to_trust_it(conn):
    """真实故障的最后一环：检索已经对了、片段也带日期，7B 模型仍然拒答。

    所以提示词里必须直接给出**算好的结论**，并明确指示优先采用 ——
    这就是这条用例要钉住的东西。
    """
    _seed_two_companies(conn)
    hits = retrieve.search(conn, "2017在那个公司", mode="keyword",
                           top_k=5, use_rerank=False)
    citations = answer._citations(conn, hits)
    structured = answer.structured_evidence(conn, "2017在那个公司")
    prompt = answer.build_user_prompt(conn, "2017在那个公司", hits,
                                      citations, structured)

    assert "结构化任职记录" in prompt
    assert "泰雷兹" in prompt
    assert "必须直接采用" in answer.SYSTEM_PROMPT


def test_ask_passes_structured_evidence_to_the_model(conn):
    class _Recorder:
        api_key = "fake"

        def __init__(self):
            self.prompts = []

        def chat(self, system, user, **kwargs):
            self.prompts.append(user)
            return "2017 年他在泰雷兹（Thales）。[1]"

    _seed_two_companies(conn)
    llm = _Recorder()
    result = answer.ask(conn, "2017在那个公司", mode="keyword", top_k=5,
                        use_rerank=False, llm=llm)

    assert len(llm.prompts) == 1
    assert "结构化任职记录" in llm.prompts[0], "算好的结论必须进提示词"
    assert "泰雷兹" in llm.prompts[0]
    assert result.degraded is False
    assert result.text.startswith("2017")


def test_ask_without_llm_still_answers_a_year_question(conn):
    """降级路径（没配 Key）也要能回答年份问题 —— 结构化记录本身就是答案。"""
    _seed_two_companies(conn)
    result = answer.ask(conn, "2017在那个公司", mode="keyword", top_k=5,
                        use_rerank=False, llm=None)

    assert result.degraded is True
    assert "泰雷兹" in result.text
