# -*- coding: utf-8 -*-
"""入库校验。

核心不变量（三条，缺一不可）：
1. **零丢失零重复**：子块文本按序拼接 == 原文
2. **可重建**：chunk_id 由内容区间推导，重复建库结果完全一致
3. **只读**：源文件 MD5 建库前后一致
"""

from __future__ import annotations

import os

import pytest

from tests.helpers import ingest, seed_chunks, source_files, store_fake_vector

from pkdb import chunker, config, db, docx_reader, extract_profile


def _chunks_of(path, start_section=None):
    name = os.path.basename(path)
    paras = docx_reader.read_docx(path)
    chunks = chunker.build_chunks(
        paras, db.doc_id_of(name), config.lang_of(name), start_section=start_section
    )
    return paras, chunks


def test_source_files_exist():
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")
    assert len(files) == config.EXPECTED_DOC_COUNT
    for path in files:
        assert path.lower().endswith(".docx")
        assert not os.path.basename(path).startswith("~$")


def test_leaf_union_equals_source():
    """子块并集必须逐字等于原文：零丢失、零重复、顺序一致。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    for path in files:
        paras, chunks = _chunks_of(path)
        assert chunker.union_text(chunks) == docx_reader.full_text(paras), \
            "%s 子块并集与原文不一致" % os.path.basename(path)


def test_leaf_ranges_partition_document():
    """子块区间必须首尾相接、不重叠、不遗漏地覆盖全文。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    for path in files:
        paras, chunks = _chunks_of(path)
        leaves = sorted(chunker.leaves(chunks), key=lambda c: c.para_start)
        cursor = 0
        for leaf in leaves:
            assert leaf.para_start == cursor, \
                "%s 子块区间不连续：期望起点 %d，实际 %d" % (
                    os.path.basename(path), cursor, leaf.para_start)
            assert leaf.para_end > leaf.para_start, "出现空区间子块"
            cursor = leaf.para_end
        assert cursor == len(paras), "子块未覆盖到文档末尾"

        # 父块必须完整包含其子块
        by_id = {c.chunk_id: c for c in chunks}
        for leaf in leaves:
            parent = by_id[leaf.parent_id]
            assert parent.para_start <= leaf.para_start
            assert parent.para_end >= leaf.para_end


def test_section_carried_across_slices():
    """切片 _2/_3 从板块中间起头时，必须继承上一份切片结尾的板块。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    families = config.group_by_family(files)
    assert len(families) == 2, "预期中文 / 英文两个切片家族"

    for family in families:
        carried = None
        for path in family:
            _, chunks = _chunks_of(path, start_section=carried)
            leaves = chunker.leaves(chunks)
            # 文件第一段就是 Heading 3 时，不该落到「抬头」
            if leaves and leaves[0].heading:
                assert leaves[0].section != config.SECTION_HEAD, \
                    "%s 首个子块丢失板块上下文" % os.path.basename(path)
            carried = chunker.last_section(chunks) or carried


def test_chunk_ids_are_deterministic():
    """同一份文件跑两次，chunk_id 完全一致（可重建）。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    path = files[0]
    _, first = _chunks_of(path)
    _, second = _chunks_of(path)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_embedding_stage_persists_and_hits_cache(conn, monkeypatch):
    """向量化只算"没算过"的块——这是免费额度保护的核心。

    用假客户端替换真实 embedder，所以本用例完全不联网。
    """
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    ingest(conn, files[0], force=True)
    targets = chunker.embeddable_rows(db.list_chunks(conn, only_leaf=True))
    assert targets, "应当存在可向量化的子块"

    called = {"n": 0}

    class FakeEmbedderClient:
        def embed(self, texts):
            called["n"] += len(texts)
            return [[0.1] * config.EMBED_DIM for _ in texts]

    from pkdb import cli
    from pkdb.llm import CallBudget

    monkeypatch.setattr(cli, "get_embedder", lambda budget=None: FakeEmbedderClient())
    monkeypatch.setattr(config, "EMBED_API_KEY", "fake-key")

    first = cli._stage_embed(conn, CallBudget(1000), do_embed=True, force=False)
    assert first["embedded"] == len(targets)
    assert db.stats(conn)["embeddings"] == len(targets)
    assert called["n"] == len(targets)

    # 第二次：全部命中缓存，不该再发起任何调用
    second = cli._stage_embed(conn, CallBudget(1000), do_embed=True, force=False)
    assert second["embedded"] == 0
    assert second["cached"] == len(targets)
    assert called["n"] == len(targets), "命中缓存的块不得重复调用 API"

    # --force 才会重算
    cli._stage_embed(conn, CallBudget(1000), do_embed=True, force=True)
    assert called["n"] == len(targets) * 2


def test_rebuild_is_idempotent(conn):
    """重复建库不产生重复行。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    path = files[0]
    ingest(conn, path, force=True)
    info1 = db.stats(conn)

    # 第二次：MD5 未变应直接跳过
    assert ingest(conn, path) is None
    info2 = db.stats(conn)
    assert info1 == info2

    # 即便强制重解析，行数也不应变化（走的是 replace 而非 append）
    ingest(conn, path, force=True)
    info3 = db.stats(conn)
    assert info3["documents"] == info2["documents"]
    assert info3["chunks"] == info2["chunks"]
    assert info3["profile_entries"] == info2["profile_entries"]


def test_source_files_not_modified(conn):
    """建库前后源文件 MD5 必须一致（只读约定）。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    before = config.snapshot_md5(files)
    for path in files:
        ingest(conn, path, force=True)
    after = config.snapshot_md5(files)
    assert before == after, "源文件被改动，违反只读约定"


def test_profile_entries_have_traceable_chunk(conn):
    """每条结构化记录都要能回连到库里真实存在的 chunk。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    for path in files:
        ingest(conn, path, force=True)

    rows = db.list_profile_entries(conn)
    assert rows, "没有抽取到任何结构化记录"
    for row in rows:
        assert db.get_chunk(conn, row["chunk_id"]) is not None, \
            "结构化记录 %s 指向了不存在的 chunk" % row["entry_id"]


def test_embeddable_skips_tiny_chunks(conn):
    """只有板块标题、没有正文的子块不送去向量化（省免费额度）。"""
    files = source_files()
    if not files:
        pytest.skip("数据源目录不可用，跳过")

    ingest(conn, files[0], force=True)
    rows = db.list_chunks(conn, only_leaf=True)
    targets = chunker.embeddable_rows(rows)
    assert 0 < len(targets) <= len(rows)
    for row in targets:
        assert len(row["text"].strip()) >= config.EMBED_MIN_CHARS


def test_vector_coverage_ignores_skipped_tiny_chunks(conn):
    """回归用例：纯标题块按设计不向量化，不能被算成"缺向量"。

    cli 的 doctor 与网页 app 都曾因分母用错（拿向量数跟**全部**子块比）而误报
    "向量未就绪"。现在两处统一走 db.vector_coverage。
    """
    seed_chunks(conn, [
        {"chunk_id": "big", "doc_id": "d1", "para_start": 0, "para_end": 2,
         "text": "负责自动化测试框架搭建与维护，覆盖接口层与 UI 层。"},
        {"chunk_id": "tiny", "doc_id": "d1", "para_start": 2, "para_end": 3,
         "text": "工作经历"},  # 纯标题，短于 EMBED_MIN_CHARS
    ])
    store_fake_vector(conn, "big", [0.1] * config.EMBED_DIM)

    coverage = db.vector_coverage(conn)
    assert coverage["need"] == 1, "分母只能算可向量化的子块"
    assert coverage["skipped"] == 1
    assert coverage["ready"] is True, "只差纯标题块时应当判定为已就绪"


def test_vector_coverage_not_ready_before_embedding(conn):
    seed_chunks(conn, [
        {"chunk_id": "big", "doc_id": "d1", "para_start": 0, "para_end": 2,
         "text": "负责自动化测试框架搭建与维护，覆盖接口层与 UI 层。"},
    ])
    coverage = db.vector_coverage(conn)
    assert coverage["got"] == 0
    assert coverage["ready"] is False


def test_vector_coverage_empty_db(conn):
    coverage = db.vector_coverage(conn)
    assert coverage == {"need": 0, "got": 0, "ready": False, "skipped": 0}


def test_extract_period_handles_both_formats():
    """中文数字型与英文月份名两种时间写法都要能解析。"""
    assert extract_profile.parse_period("2016.10 – 2022.12") == ("2016-10", "2022-12")
    assert extract_profile.parse_period("Oct 2016 - Dec 2022") == ("2016-10", "2022-12")
    assert extract_profile.parse_period("Mar 2004 ~ Apr 2005") == ("2004-03", "2005-04")
    # 「至今」按语言给出不同写法
    assert extract_profile.parse_period("Jan 2023 – Present", "en") == ("2023-01", "present")
    assert extract_profile.parse_period("2023.01 – 至今", "zh") == ("2023-01", "至今")
    assert extract_profile.parse_period("没有任何时间") == (None, None)
