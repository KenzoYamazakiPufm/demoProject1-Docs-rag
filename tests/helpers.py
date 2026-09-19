# -*- coding: utf-8 -*-
"""测试共用工具（放在包内，便于用 ``from tests.helpers import ...`` 引用）。

关键约定：**测试全程不联网**——向量相关的用例一律注入假 embedder，
所以有没有 API Key 都能跑。
"""

from __future__ import annotations

import os

from pkdb import chunker, config, db, docx_reader, extract_profile


def source_files():
    """真实数据源里的 6 个切片；缺失时返回空列表（用例会 skip）。"""
    return config.source_docx_files()


def ingest(conn, path, start_section=None, force=False):
    """把一份 docx 走完整入库流程，返回 chunks（已入库则返回 None）。"""
    name = os.path.basename(path)
    doc_id = db.doc_id_of(name)
    lang = config.lang_of(name)
    md5 = config.md5_file(path)

    if not force and db.doc_md5(conn, doc_id) == md5:
        return None

    paras = docx_reader.read_docx(path)
    chunks = chunker.build_chunks(paras, doc_id, lang, start_section=start_section)
    db.upsert_document(conn, {
        "doc_id": doc_id, "file_name": name, "lang": lang,
        "slice_no": config.slice_no_of(name), "md5": md5,
        "para_count": len(paras),
    })
    db.replace_chunks(conn, doc_id, [c.as_row() for c in chunks])
    db.replace_profile_entries(
        conn, doc_id, extract_profile.extract_entries(chunks, doc_id, lang)
    )
    conn.commit()
    return chunks


def seed_chunks(conn, rows):
    """直接写入一组 chunk，用于检索 / 回答层的离线用例。

    rows: list[dict(chunk_id, doc_id, text, ...)]，缺省字段会自动补。
    """
    by_doc = {}
    for row in rows:
        # 检索只认"子块"（parent_id 非空），所以种子数据必须挂在某个父块下
        row.setdefault("parent_id", "parent:%s" % row["chunk_id"])
        row.setdefault("lang", "zh")
        row.setdefault("section", config.SECTION_WORK)
        row.setdefault("heading", None)
        row.setdefault("n_chars", len(row["text"]))
        if db.get_document(conn, row["doc_id"]) is None:
            db.upsert_document(conn, {
                "doc_id": row["doc_id"], "file_name": row["doc_id"] + ".docx",
                "lang": row["lang"], "slice_no": 1, "md5": "x", "para_count": 10,
            })
        by_doc.setdefault(row["doc_id"], []).append(row)
    for doc_id, group in by_doc.items():
        db.replace_chunks(conn, doc_id, group)
    conn.commit()


def store_fake_vector(conn, chunk_id, vec):
    db.upsert_embeddings(conn, [{
        "chunk_id": chunk_id, "model": config.EMBED_MODEL,
        "dim": len(vec), "vector": db.pack_vector(vec),
    }])
    conn.commit()


class FakeEmbedder:
    """按预先给好的映射返回向量，完全离线。"""

    def __init__(self, mapping):
        self.mapping = dict(mapping)

    def embed(self, texts):
        return [self.mapping.get(text, [0.0] * 3) for text in texts]
