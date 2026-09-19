# -*- coding: utf-8 -*-
"""三路检索与融合。

设计要点：
1. **结构化筛选是前置过滤器**（SQL WHERE），不参与排名打分
2. BM25（字面）与向量余弦（语义）各出一份排名，用 RRF 融合
3. **年份路**：问句里出现年份时，用「块 → 年份」派生索引精确召回。
   前两路都是"相似度"，只有它是**逻辑运算**（某年是否落在某个区间内）——
   简历只写 ``2016.10 – 2022.12``、从不出现"2017"这个字面，所以少了这一路
   就必然答不出"2017 在哪个公司"。它带权重，理由见 YEAR_CHANNEL_WEIGHT。
4. 可选再用 reranker 精排（两两成对看，更准但更慢，所以只用在粗筛后的少量候选上）
5. 数据量在千级以下，向量检索用 numpy 暴力扫描即可，**不引入向量数据库**
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from . import config, db

# --------------------------------------------------------------- 分词

try:  # jieba 是可选依赖，缺失时退化为正则切分
    import jieba  # type: ignore

    jieba.setLogLevel(60)
    _JIEBA = jieba
except Exception:  # pragma: no cover - 环境缺依赖时走退化路径
    _JIEBA = None

_PUNCT = set("\uff0c\u3002\u3001\uff1b\uff1a\uff1f\uff01\uff08\uff09\u3010\u3011"
             "\u300a\u300b\u201c\u201d\u2018\u2019,.;:?!()[]{}<>\"'`~@#$%^&*+=|\\/ "
             "\t\n\r-\u2014_")
_TOKEN_RE = re.compile(r"[a-z0-9_+#.]+|[\u4e00-\u9fff]")


def tokenize(text: str):
    """中英混排分词：优先 jieba，缺失时正则回退。"""
    if not text:
        return []
    low = text.lower()
    raw = _JIEBA.lcut(low) if _JIEBA is not None else _TOKEN_RE.findall(low)
    return [tok for tok in raw if tok.strip() and not all(ch in _PUNCT for ch in tok)]


# --------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    file_name: str
    lang: str
    section: str
    heading: str = None
    para_start: int = 0
    para_end: int = 0
    text: str = ""
    score: float = 0.0
    source: str = "rrf"          # bm25 | vector | rrf | rerank | filter

    @property
    def para_range(self) -> str:
        return "%d-%d" % (self.para_start, self.para_end - 1)


# --------------------------------------------------------------- 关键词路

def _floor_zero_idf(model, corpus_size: int) -> None:
    """把非正的 IDF 抬到一个恒正的小值。

    ``BM25Okapi`` 的 IDF = ``log(N - df + 0.5) - log(df + 0.5)``，在**小语料**上会退化：

    - 词只出现在 1 篇、而语料只有 2 篇时，IDF 恰好等于 **0** → 全部得分归零，
      关键词路整体失效（本项目只有 30 多个子块，正是这种小语料）
    - 词出现在多数文档时 IDF 变**负** → 越常见反而越扣分

    两者都不是我们要的行为，所以统一抬到 ``log(1 + 1/(N + 0.5))``：
    恒为正、且远小于稀有词，排序不受影响；同时**没有词命中的文档得分仍为 0**，
    会被下面的 ``s > 0`` 过滤掉。

    （试过``BM25Plus``：它给每个查询词都加一个 ``delta`` 基线分，
    结果是所有文档得分都大于 0，无关块也会被全部召回。）
    """
    floor = math.log(1.0 + 1.0 / (corpus_size + 0.5))
    for word, value in list(model.idf.items()):
        if value <= 0:
            model.idf[word] = floor


def bm25_rank(ids, texts, query: str, top_n: int = None):
    """BM25 打分排序，返回 ``[(chunk_id, score)]`` 降序（只保留有词命中的文档）。"""
    tokens_query = tokenize(query)
    corpus = [tokenize(t) for t in texts]
    if not tokens_query or not corpus:
        return []

    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except Exception:  # pragma: no cover - 缺依赖时退化为内含实现
        return _bm25_fallback(ids, corpus, tokens_query, top_n)

    bm25 = BM25Okapi(corpus)
    _floor_zero_idf(bm25, len(corpus))
    scores = bm25.get_scores(tokens_query)
    pairs = [(cid, float(s)) for cid, s in zip(ids, scores) if s > 0]
    pairs.sort(key=lambda item: (-item[1], item[0]))
    return pairs[:top_n] if top_n else pairs


def _bm25_fallback(ids, corpus, tokens_query, top_n, k1: float = 1.5, b: float = 0.75):
    """rank_bm25 缺失时的等价实现，保证离线可跑。

    IDF 用 ``log(1 + (N - df + 0.5) / (df + 0.5))``，恒为正——与 BM25Plus 一样
    不会在小语料上退化。
    """
    n_docs = len(corpus)
    avg_len = sum(len(d) for d in corpus) / n_docs if n_docs else 0.0
    df = {}
    for doc in corpus:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1

    scored = []
    for doc_id, doc in zip(ids, corpus):
        length = len(doc) or 1
        score = 0.0
        for term in tokens_query:
            tf = doc.count(term)
            if not tf:
                continue
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / (avg_len or 1)))
        if score > 0:
            scored.append((doc_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:top_n] if top_n else scored


# --------------------------------------------------------------- 语义路

def vector_rank(ids, matrix, query_vec, top_n: int = None):
    """余弦相似度暴力扫描。ids 与 matrix 行一一对应。"""
    if not ids or matrix.size == 0 or query_vec is None:
        return []
    query = np.asarray(query_vec, dtype="<f4").ravel()
    if query.size != matrix.shape[1]:
        return []
    matrix_norm = np.linalg.norm(matrix, axis=1)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0.0:
        return []
    sims = (matrix @ query) / (matrix_norm * query_norm + 1e-12)
    order = np.argsort(-sims)
    pairs = [(ids[i], float(sims[i])) for i in order]
    return pairs[:top_n] if top_n else pairs


# --------------------------------------------------------------- 年份路
# 精确路：问句里出现年份时，召回"时间区间覆盖了该年份"的块。
# 它解决的是**逻辑运算**（2016.10 <= 2017 <= 2022.12），而语义相似度天生
# 做不了这件事 —— 简历里从不出现"2017"这个字面，关键词路必然全灭，
# 向量路则会把 2017 当成与 2016 最像的数字、召回错年份的内容。

# 用 (?<!\d) / (?!\d) 卡住边界，避免把 "20165" 或长编号里的片段当年份
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def years_in_query(query: str):
    """抽出去重升序的年份列表；没有年份返回空列表。"""
    return sorted({int(year) for year in _YEAR_RE.findall(query or "")})


def year_rank(conn, years, allowed_ids, top_n: int = None):
    """年份路榜单，返回 ``[(chunk_id, score)]``。

    ``allowed_ids`` 是经过 section / lang 预过滤后的候选集 —— 与另外两路
    保持同一个候选范围，避免过滤器被绕过。
    """
    if not years:
        return []
    allowed = set(allowed_ids)
    pairs = [(cid, 1.0) for cid in db.chunk_ids_for_years(conn, years) if cid in allowed]
    return pairs[:top_n] if top_n else pairs


# --------------------------------------------------------------- RRF 融合

def rrf_fuse(rankings, k: int = None, top_n: int = None, weights=None):
    """倒数排名融合：不比分数，只比名次，奖励"多路都看好"的段落。

    ``weights`` 给每个榜单一个权重（默认 1.0），用来区分**精确命中**与
    **近似命中**：年份路是精确的，权重 > 1；理由与取值见
    ``config.YEAR_CHANNEL_WEIGHT``——真实故障里 BM25 与向量会一起指错，
    正确的那块却不会出现在它们榜单里，1 票对 2 票不加权必输。

    权重个数与榜单个数不一致时**退回全 1**，而不是错配着算 —— 宁可少一点
    效果，也不要静默算出一份谁都不理解的排名。
    """
    kk = config.RRF_K if k is None else k
    scale = list(weights) if weights is not None else [1.0] * len(rankings)
    if len(scale) != len(rankings):
        scale = [1.0] * len(rankings)

    scores = {}
    for ranking, weight in zip(rankings, scale):
        for rank, item in enumerate(ranking, 1):
            chunk_id = item[0]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (kk + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:top_n] if top_n else ordered


# --------------------------------------------------------------- 主入口

def _doc_name_map(conn) -> dict:
    return {
        row["doc_id"]: row["file_name"]
        for row in conn.execute("SELECT doc_id, file_name FROM documents")
    }


def _query_vector(query: str, embedder):
    """取查询向量；没有 embedder 或调用失败时该路自动缺席（不阻塞检索）。"""
    if embedder is None:
        return None
    try:
        vectors = embedder.embed([query])
    except Exception as exc:  # 网络 / 限流 / 缺 Key 都不该让检索崩掉
        import logging
        logging.getLogger("pkdb").warning("查询向量化失败，本次仅用关键词路：%s", exc)
        return None
    return vectors[0] if vectors else None


def search(conn, query: str, *, mode: str = "hybrid", lang: str = None,
           section: str = None, top_k: int = None, use_rerank: bool = None,
           embedder=None, reranker=None, budget=None):
    """返回 ``list[Hit]``（按融合得分降序，长度 <= top_k）。"""
    top_k = config.TOP_K if top_k is None else top_k
    mode = (mode or "hybrid").lower()
    if not (query or "").strip():
        return []

    # ① 结构化预过滤：SQL WHERE 先砍候选，不参与排名
    rows = db.list_chunks(conn, section=section, lang=lang, only_leaf=True)
    rows = [r for r in rows if (r["text"] or "").strip()]
    if not rows:
        return []

    ids = [r["chunk_id"] for r in rows]
    texts = [r["text"] for r in rows]

    rankings = []
    if mode in ("keyword", "bm25", "hybrid", "all"):
        kw = bm25_rank(ids, texts, query, top_n=config.FUSION_TOP_K)
        if kw:
            rankings.append(("bm25", kw))

    if mode in ("semantic", "vector", "hybrid", "all"):
        # 只对候选集内的向量排序；库为空或没有 Key 时该路自动缺席
        vec_ids, matrix = db.load_vectors(conn, config.EMBED_MODEL, config.EMBED_DIM)
        row_map = {r["chunk_id"]: i for i, r in enumerate(rows)}
        keep = [(row_map[cid], cid, i) for i, cid in enumerate(vec_ids) if cid in row_map]
        if keep:
            sub_ids = [cid for _, cid, _ in keep]
            sub_matrix = matrix[[i for _, _, i in keep]]
            query_vec = _query_vector(query, embedder)
            if query_vec is not None:
                vec = vector_rank(sub_ids, sub_matrix, query_vec, top_n=config.FUSION_TOP_K)
                if vec:
                    rankings.append(("vector", vec))

    # 年份路：**与 mode 无关**。它不是"相似度"路，而是由结构化表推导出来的
    # 精确信号；只在 hybrid 下生效的话，`--mode keyword` 又会重现"2017 答不出来"。
    yr = year_rank(conn, years_in_query(query), ids, top_n=config.FUSION_TOP_K)
    if yr:
        rankings.append(("year", yr))

    if not rankings:
        return []

    if len(rankings) == 1:
        source = rankings[0][0]
        fused = list(rankings[0][1])[:config.FUSION_TOP_K]
    else:
        source = "rrf"
        weights = [config.YEAR_CHANNEL_WEIGHT if name == "year" else 1.0
                   for name, _ in rankings]
        fused = rrf_fuse([ranking for _, ranking in rankings],
                         config.RRF_K, top_n=config.FUSION_TOP_K, weights=weights)

    # ② 可选精排：只对粗筛后的少量候选调用
    want_rerank = config.RERANK_ENABLED if use_rerank is None else use_rerank
    if want_rerank and reranker is not None and getattr(reranker, "enabled", False):
        chunk_map = db.get_chunks_by_ids(conn, [cid for cid, _ in fused])
        docs = [chunk_map[cid]["text"] if cid in chunk_map else "" for cid, _ in fused]
        try:
            ordered = reranker.rerank(query, docs, top_n=len(docs))
        except Exception:
            ordered = []
        if ordered:
            fused = [(fused[idx][0], score) for idx, score in ordered]
            source = "rerank"

    # ③ 组装 Hit
    doc_names = _doc_name_map(conn)
    chunk_map = db.get_chunks_by_ids(conn, [cid for cid, _ in fused[:top_k]])
    hits = []
    for chunk_id, score in fused[:top_k]:
        row = chunk_map.get(chunk_id)
        if row is None:
            continue
        hits.append(
            Hit(
                chunk_id=chunk_id,
                doc_id=row["doc_id"],
                file_name=doc_names.get(row["doc_id"], row["doc_id"]),
                lang=row["lang"],
                section=row["section"],
                heading=row["heading"],
                para_start=row["para_start"],
                para_end=row["para_end"],
                text=row["text"],
                score=float(score),
                source=source,
            )
        )
    return hits


# ------------------------------------------------------- 结构化精确筛选

def filter_entries(conn, kind: str = None, title_like: str = None,
                   start_from: str = None, start_to: str = None,
                   tech_like: str = None):
    """按结构化字段精确筛选（走 SQL，不让大模型猜）。"""
    sql = "SELECT * FROM profile_entries WHERE 1 = 1"
    params = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if title_like:
        sql += " AND title LIKE ?"
        params.append("%" + title_like + "%")
    if start_from:
        sql += " AND COALESCE(start_ym, '') >= ?"
        params.append(start_from)
    if start_to:
        sql += " AND COALESCE(start_ym, '') <= ?"
        params.append(start_to)
    if tech_like:
        sql += " AND COALESCE(tech, '') LIKE ?"
        params.append("%" + tech_like + "%")
    sql += " ORDER BY COALESCE(start_ym, ''), title"
    return conn.execute(sql, params).fetchall()
