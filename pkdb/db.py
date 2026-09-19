# -*- coding: utf-8 -*-
"""SQLite 读写层。

职责：
- 初始化 schema.sql
- 幂等 upsert（``ON CONFLICT DO UPDATE``）：重复建库不会产生重复行
- 按文件 MD5 判断是否需要重新解析
- 向量 float32 BLOB 的打包 / 还原
- 统计查询

不含任何业务逻辑与网络调用。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime

import numpy as np

from . import config


# ------------------------------------------------------------------ 连接

def connect(path: str = None) -> sqlite3.Connection:
    """打开数据库连接（默认指向 pkdb/data/pkdb.sqlite3）。"""
    target = path or config.DB_PATH
    if target != ":memory:":
        config.ensure_data_dir()
    # check_same_thread=False：Streamlit 会在不同线程 / 重跑之间复用同一个连接
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """执行 schema.sql（全部 CREATE IF NOT EXISTS，可重复调用）。"""
    with open(config.SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()


def open_db(path: str = None) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


# ------------------------------------------------------------- 向量编解码

def pack_vector(vec) -> bytes:
    """float32 小端序列，numpy frombuffer 可直接还原。"""
    return np.asarray(vec, dtype="<f4").tobytes()


def unpack_vector(blob) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


# ------------------------------------------------------------------ 工具

def doc_id_of(file_name: str) -> str:
    """稳定标识：文件名的 sha1 前 12 位。"""
    return hashlib.sha1(file_name.encode("utf-8")).hexdigest()[:12]


def chunk_id_of(doc_id: str, para_start: int, para_end: int, role: str = "C") -> str:
    """稳定标识：由 (doc_id, 段落区间, 父/子角色) 推导 —— 重建索引结果完全一致。

    ``role`` 必须参与哈希：板块级父块与"无子块的父块"可能占用同一段落区间，
    只用区间会产生主键冲突。
    """
    raw = "%s:%d:%d:%s" % (doc_id, para_start, para_end, role)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------- 写入 documents

def upsert_document(conn, doc) -> None:
    conn.execute(
        """
        INSERT INTO documents (doc_id, file_name, lang, slice_no, md5, para_count, ingested_at)
        VALUES (:doc_id, :file_name, :lang, :slice_no, :md5, :para_count, :ingested_at)
        ON CONFLICT(doc_id) DO UPDATE SET
            file_name  = excluded.file_name,
            lang       = excluded.lang,
            slice_no   = excluded.slice_no,
            md5        = excluded.md5,
            para_count = excluded.para_count,
            ingested_at= excluded.ingested_at
        """,
        {
            "doc_id": doc["doc_id"],
            "file_name": doc["file_name"],
            "lang": doc["lang"],
            "slice_no": doc.get("slice_no"),
            "md5": doc["md5"],
            "para_count": doc.get("para_count"),
            "ingested_at": _now(),
        },
    )


def get_document(conn, doc_id: str):
    return conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()


def doc_md5(conn, doc_id: str):
    """返回库里记录的该文件 MD5；未入库返回 None。"""
    row = conn.execute("SELECT md5 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return row["md5"] if row else None


# --------------------------------------------------------------- 写入 chunks

def replace_chunks(conn, doc_id: str, rows) -> int:
    """整份替换某文档的 chunk（先删后插），保证重复运行不产生重复行。

    返回写入的 chunk 条数。
    """
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        """
        INSERT INTO chunks
            (chunk_id, doc_id, parent_id, section, heading, lang,
             para_start, para_end, text, n_chars)
        VALUES
            (:chunk_id, :doc_id, :parent_id, :section, :heading, :lang,
             :para_start, :para_end, :text, :n_chars)
        """,
        list(rows),
    )
    return conn.execute(
        "SELECT COUNT(*) AS c FROM chunks WHERE doc_id = ?", (doc_id,)
    ).fetchone()["c"]


def list_chunks(conn, doc_id: str = None, section: str = None, lang: str = None,
                only_leaf: bool = False):
    """按条件列出 chunk，按 (doc_id, para_start) 稳定排序。"""
    sql = "SELECT * FROM chunks WHERE 1 = 1"
    params = []
    if doc_id:
        sql += " AND doc_id = ?"
        params.append(doc_id)
    if section:
        sql += " AND section = ?"
        params.append(section)
    if lang and lang != "all":
        sql += " AND lang = ?"
        params.append(lang)
    if only_leaf:
        sql += " AND parent_id IS NOT NULL"
    sql += " ORDER BY doc_id, para_start, para_end"
    return conn.execute(sql, params).fetchall()


def get_chunk(conn, chunk_id: str):
    return conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()


def last_section(conn, doc_id: str):
    """该文档最后一段所属的板块；库里没有则返回 None。

    供建库时做"切片间板块继承"：跳过的切片也要能从库里取到它结尾的板块。
    """
    row = conn.execute(
        "SELECT section FROM chunks WHERE doc_id = ? AND parent_id IS NULL "
        "ORDER BY para_end DESC LIMIT 1",
        (doc_id,),
    ).fetchone()
    return row["section"] if row else None


def get_chunks_by_ids(conn, chunk_ids):
    ids = list(chunk_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute("SELECT * FROM chunks WHERE chunk_id IN (%s)" % marks, ids)
    return {row["chunk_id"]: row for row in rows}


# ----------------------------------------------------------- 写入 embeddings

def upsert_embeddings(conn, rows) -> int:
    """写入 / 更新向量。rows: dict(chunk_id, model, dim, vector<bytes>)"""
    conn.executemany(
        """
        INSERT INTO embeddings (chunk_id, model, dim, vector, created_at)
        VALUES (:chunk_id, :model, :dim, :vector, :created_at)
        ON CONFLICT(chunk_id) DO UPDATE SET
            model      = excluded.model,
            dim        = excluded.dim,
            vector     = excluded.vector,
            created_at = excluded.created_at
        """,
        [dict(row, created_at=_now()) for row in rows],
    )
    return len(rows)


def cached_chunk_ids(conn, model: str, dim: int = None):
    """已缓存该模型向量的 chunk_id 集合（命中缓存即不再调用 API）。"""
    sql = "SELECT chunk_id, dim FROM embeddings WHERE model = ?"
    rows = conn.execute(sql, (model,)).fetchall()
    if dim is None:
        return {row["chunk_id"] for row in rows}
    return {row["chunk_id"] for row in rows if row["dim"] == dim}


def drop_embeddings_of_model(conn, model: str) -> int:
    cur = conn.execute("DELETE FROM embeddings WHERE model = ?", (model,))
    return cur.rowcount


def load_vectors(conn, model: str, dim: int = None):
    """载入全部向量。返回 (chunk_ids:list, matrix:np.ndarray[N, d])。

    维度与当前配置不一致的旧缓存会被忽略（自动失效）。
    """
    rows = conn.execute(
        "SELECT chunk_id, dim, vector FROM embeddings WHERE model = ?", (model,)
    ).fetchall()
    ids, vecs = [], []
    for row in rows:
        if dim is not None and row["dim"] != dim:
            continue
        ids.append(row["chunk_id"])
        vecs.append(unpack_vector(row["vector"]))
    if not vecs:
        return [], np.zeros((0, 0), dtype="<f4")
    return ids, np.vstack(vecs)


# ------------------------------------------------------ 写入 profile_entries

def replace_profile_entries(conn, doc_id: str, rows) -> int:
    """替换某文档派生的结构化记录（依据该文档的 chunk 归属删除后再插）。"""
    conn.execute(
        """
        DELETE FROM profile_entries
        WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE doc_id = ?)
        """,
        (doc_id,),
    )
    payload = list(rows)
    if payload:
        conn.executemany(
            """
            INSERT INTO profile_entries
                (entry_id, kind, title, role, start_ym, end_ym, tech, chunk_id, raw)
            VALUES
                (:entry_id, :kind, :title, :role, :start_ym, :end_ym, :tech, :chunk_id, :raw)
            """,
            payload,
        )
    return len(payload)


def list_profile_entries(conn, kind: str = None):
    sql = "SELECT * FROM profile_entries WHERE 1 = 1"
    params = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY COALESCE(start_ym, ''), title"
    return conn.execute(sql, params).fetchall()


# ------------------------------------------------- 派生索引：块 → 年份
# 用途见 schema.sql 里 chunk_years 的注释：让"2017"这种**区间内的年份**
# 可以被精确召回。本节的函数**不联网、不改 chunk 文本**，所以重建它不会
# 让任何一条向量失效。

# 未结束的时间段写法（中文 / 英文）
_OPEN_ENDED = ("\u81f3\u4eca", "present", "now")
# 防御脏数据：正常任职/项目不会跨越 60 年，超出就当作解析出错
_MAX_YEAR_SPAN = 60


def year_of_ym(ym: str):
    """从 ``YYYY-MM`` / ``YYYY`` 里取年份，取不到返回 None。"""
    if not ym:
        return None
    match = re.match(r"\s*(\d{4})", str(ym))
    return int(match.group(1)) if match else None


def years_of_period(start_ym: str, end_ym: str, now_year: int = None):
    """把 ``2016-10 ~ 2022-12`` 展开成 ``[2016, 2017, ..., 2022]``。

    终点写成"至今 / present"时展开到 ``now_year``（默认今年）。
    起点缺失、终点无法识别、终点早于起点、跨度异常大 —— 一律返回空列表：
    **宁可这条记录不参与年份召回，也不要瞎猜一个年份。**
    """
    start = year_of_ym(start_ym)
    if start is None:
        return []

    end_text = (end_ym or "").strip()
    if any(token in end_text.lower() for token in _OPEN_ENDED):
        end = now_year or datetime.now().year
    else:
        end = year_of_ym(end_text)

    if end is None or end < start or end - start > _MAX_YEAR_SPAN:
        return []
    return list(range(start, end + 1))


def rebuild_chunk_years(conn) -> int:
    """由 ``profile_entries`` 重建「块 → 年份」派生索引，返回写入的 (块,年) 条数。

    整表重建（先删后插）：它是纯派生物，重建比增量便宜得多，也不会与源数据脱节。
    """
    conn.execute("DELETE FROM chunk_years")
    rows = []
    # JOIN chunks：只索引**确实存在**的块。结构化记录万一残留孤儿行
    # （比如文档被清理掉），也不该在索引里留下查不到的幽灵。
    for entry in conn.execute(
        "SELECT e.chunk_id AS chunk_id, e.start_ym AS start_ym, e.end_ym AS end_ym "
        "FROM profile_entries e JOIN chunks c ON c.chunk_id = e.chunk_id "
        "WHERE e.chunk_id IS NOT NULL AND e.chunk_id <> ''"
    ):
        for year in years_of_period(entry["start_ym"], entry["end_ym"]):
            rows.append({"chunk_id": entry["chunk_id"], "year": year})
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO chunk_years (chunk_id, year) "
            "VALUES (:chunk_id, :year)",
            rows,
        )
    conn.commit()
    return len(rows)


def chunk_ids_for_years(conn, years):
    """返回覆盖了给定年份的 chunk_id，按「命中年份数降序、chunk_id 升序」排序。

    用 JOIN 过滤掉 chunks 表里已不存在的 chunk_id —— 索引与数据万一脱节，
    也不该召回幽灵块。
    """
    wanted = sorted({int(y) for y in years})
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))
    rows = conn.execute(
        """
        SELECT cy.chunk_id AS chunk_id, COUNT(*) AS matched
        FROM chunk_years cy
        JOIN chunks c ON c.chunk_id = cy.chunk_id
        WHERE cy.year IN (%s)
        GROUP BY cy.chunk_id
        ORDER BY matched DESC, cy.chunk_id ASC
        """ % marks,
        wanted,
    ).fetchall()
    return [row["chunk_id"] for row in rows]


def entries_covering_years(conn, years, lang: str = None):
    """返回时间区间覆盖了给定年份的结构化记录（company / project）。

    走 ``chunk_years`` 派生索引 —— 与检索层用的是**同一份事实**，
    不会出现"检索说 A、结构化说 B"的分叉。

    ``lang`` 用来去掉中英双份重复：同一个单位在 CN / EN 文档里各存了一条，
    两种语言混在一起会把"2017 属于哪家"说出四遍，反而干扰模型。
    首选语言取不到记录时退回全语言 —— 宁可啰嗦，也别答不出来。
    """
    wanted = sorted({int(y) for y in years})
    if not wanted:
        return []

    marks = ",".join("?" * len(wanted))
    sql = (
        "SELECT DISTINCT e.kind AS kind, e.title AS title, e.role AS role, "
        "       e.start_ym AS start_ym, e.end_ym AS end_ym, d.lang AS lang "
        "FROM profile_entries e "
        "JOIN chunk_years cy ON cy.chunk_id = e.chunk_id "
        "JOIN chunks c ON c.chunk_id = e.chunk_id "
        "JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE cy.year IN (%s)" % marks
    )
    params = list(wanted)
    if lang and lang != "all":
        sql += " AND d.lang = ?"
        params.append(lang)
    sql += " ORDER BY e.kind, COALESCE(e.start_ym, ''), e.title"

    rows = conn.execute(sql, params).fetchall()
    if rows or not lang or lang == "all":
        return rows
    return entries_covering_years(conn, wanted, lang=None)


def year_index_stats(conn) -> dict:
    """年份索引覆盖情况——**唯一真相源**，doctor 与 app 都调它。

    与 ``vector_coverage`` 是同一个教训：同一个判定口径写在两处必然分叉，
    所以这里只算一次，谁要显示谁来调。
    """
    with_period = conn.execute(
        "SELECT COUNT(*) AS c FROM profile_entries "
        "WHERE COALESCE(start_ym, '') <> ''"
    ).fetchone()["c"]
    indexed = conn.execute(
        "SELECT COUNT(DISTINCT chunk_id) AS c FROM chunk_years"
    ).fetchone()["c"]
    year_rows = conn.execute("SELECT COUNT(*) AS c FROM chunk_years").fetchone()["c"]
    span = conn.execute(
        "SELECT MIN(year) AS lo, MAX(year) AS hi FROM chunk_years"
    ).fetchone()
    return {
        "entries_with_period": with_period,
        "indexed_chunks": indexed,
        "year_rows": year_rows,
        "min_year": span["lo"],
        "max_year": span["hi"],
        # 没有带时间的记录时谈不上"缺失"，不该报 FAIL
        "ready": indexed > 0 or with_period == 0,
    }


# ------------------------------------------------------------------ 统计

def vector_coverage(conn, min_chars: int = None) -> dict:
    """向量覆盖情况——**唯一真相源**，app 与 doctor 都用它，避免判定口径分叉。

    分母只算"可向量化的子块"：纯标题块 / 空白块按 ``EMBED_MIN_CHARS`` 的设计
    本来就不送去向量化，拿它们跟向量条数比会永远判成"未就绪"。
    """
    floor = config.EMBED_MIN_CHARS if min_chars is None else min_chars
    rows = conn.execute(
        "SELECT text FROM chunks WHERE parent_id IS NOT NULL"
    ).fetchall()
    need = sum(1 for row in rows if len((row["text"] or "").strip()) >= floor)
    got = conn.execute("SELECT COUNT(*) AS c FROM embeddings").fetchone()["c"]
    return {"need": need, "got": got, "ready": need > 0 and got >= need,
            "skipped": len(rows) - need}


def stats(conn) -> dict:
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    by_section = {
        row["section"]: row["c"]
        for row in conn.execute(
            "SELECT section, COUNT(*) AS c FROM chunks GROUP BY section ORDER BY c DESC"
        )
    }
    by_kind = {
        row["kind"]: row["c"]
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS c FROM profile_entries GROUP BY kind ORDER BY c DESC"
        )
    }
    last = conn.execute("SELECT MAX(ingested_at) AS t FROM documents").fetchone()["t"]
    return {
        "documents": one("SELECT COUNT(*) FROM documents"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "leaf_chunks": one("SELECT COUNT(*) FROM chunks WHERE parent_id IS NOT NULL"),
        "embeddings": one("SELECT COUNT(*) FROM embeddings"),
        "profile_entries": one("SELECT COUNT(*) FROM profile_entries"),
        "by_section": by_section,
        "by_kind": by_kind,
        "ingested_at": last,
    }


def reset(conn) -> None:
    """清空全部业务数据（保留表结构）。"""
    for table in ("embeddings", "profile_entries", "chunks", "documents"):
        conn.execute("DELETE FROM %s" % table)
    conn.commit()
