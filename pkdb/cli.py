# -*- coding: utf-8 -*-
"""命令行入口。

    python -m pkdb.cli build         建库（解析 / 切分 / 抽字段 / 向量化）
    python -m pkdb.cli search "关键词" 纯检索，不调大模型
    python -m pkdb.cli ask "问题"     检索 + 大模型组织语言
    python -m pkdb.cli stats         看库内统计
    python -m pkdb.cli doctor        自检（路径 / Key / 连通性）

中文参数在 PowerShell 里可能被编码搞乱，因此：
- `--section` 支持英文别名（head/overview/strength/project/work/edu）
- 查询句子可用 `--query-file 文件.txt` 或从标准输入管道传入
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import answer as answer_mod
from . import chunker, config, db, docx_reader, extract_profile, retrieve
from .docx_reader import preview
from .llm import CallBudget, chunked, get_embedder, get_llm, get_reranker

log = logging.getLogger("pkdb")


# --------------------------------------------------------------- 终端输出

_CODES = {
    "dim": "\033[2m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[36m", "bold": "\033[1m",
    "reset": "\033[0m",
}
_COLOR = {"enabled": True}


def paint(text, *styles):
    """彩色输出；`--no-color` 或 NO_COLOR 环境变量可关闭。"""
    if not _COLOR["enabled"] or not styles:
        return str(text)
    prefix = "".join(_CODES.get(style, "") for style in styles)
    return "%s%s%s" % (prefix, text, _CODES["reset"])


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _stage(idx: int, total: int, title: str, detail: str = "", seconds: float = None):
    line = "[%d/%d] %s" % (idx, total, title)
    dots = "." * max(2, 30 - len(title))
    tail = detail
    if seconds is not None:
        tail = "%s  %.2fs" % (detail, seconds)
    print("%s %s %s" % (paint(line, "bold"), paint(dots, "dim"), tail))


def _mask(secret: str) -> str:
    if not secret:
        return paint("(未配置)", "yellow")
    if len(secret) <= 8:
        return secret[:2] + "*" * 4
    return "%s...%s" % (secret[:6], secret[-4:])


# --------------------------------------------------------------- 建库

def _collect_source_files():
    files = config.source_docx_files()
    if not files:
        raise SystemExit(
            "数据源目录不存在或没有 docx：\n  %s\n"
            "请确认该目录下有 6 个切片文件。" % config.SRC_DIR
        )
    return files


def _prune_missing(conn, keep_names) -> int:
    """删掉库里已不存在于数据源的文件（保证重建索引口径一致）。"""
    removed = 0
    for row in conn.execute("SELECT doc_id, file_name FROM documents"):
        if row["file_name"] not in keep_names:
            doc_id = row["doc_id"]
            conn.execute("DELETE FROM profile_entries WHERE chunk_id IN "
                         "(SELECT chunk_id FROM chunks WHERE doc_id = ?)", (doc_id,))
            conn.execute("DELETE FROM embeddings WHERE chunk_id IN "
                         "(SELECT chunk_id FROM chunks WHERE doc_id = ?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            removed += 1
    return removed


def _stage_embed(conn, budget, do_embed: bool, force: bool) -> dict:
    """向量化：只算没有缓存的块，命中缓存不再调用 API。"""
    targets = chunker.embeddable_rows(db.list_chunks(conn, only_leaf=True))
    cached = set() if force else db.cached_chunk_ids(conn, config.EMBED_MODEL, config.EMBED_DIM)
    todo = [c for c in targets if c["chunk_id"] not in cached]

    report = {"targets": len(targets), "cached": len(targets) - len(todo),
              "embedded": 0, "api_calls": 0, "skipped": False}

    if not do_embed:
        report["skipped"] = True
        return report
    if not config.EMBED_API_KEY:
        report["skipped"] = True
        report["reason"] = "未配置 PKDB_EMBED_API_KEY"
        return report
    if not todo:
        return report

    embedder = get_embedder(budget=budget)
    batch_size = max(config.EMBED_BATCH, 32)
    for group in chunked(todo, batch_size * 4):
        vectors = embedder.embed([row["text"] for row in group])
        actual_dim = len(vectors[0]) if vectors else 0
        if actual_dim and actual_dim != config.EMBED_DIM:
            print(paint("  警告：返回维度 %d 与配置 PKDB_EMBED_DIM=%d 不一致，"
                        "请把 .env 里的 PKDB_EMBED_DIM 改成 %d"
                        % (actual_dim, config.EMBED_DIM, actual_dim), "yellow"))
        db.upsert_embeddings(conn, [
            {"chunk_id": row["chunk_id"], "model": config.EMBED_MODEL,
             "dim": len(vec), "vector": db.pack_vector(vec)}
            for row, vec in zip(group, vectors)
        ])
        conn.commit()
        report["embedded"] += len(vectors)
        print(paint("    已向量化 %d / %d" % (report["embedded"], len(todo)), "dim"))

    report["api_calls"] = budget.used if budget else 0
    return report


def cmd_build(args) -> int:
    t0 = time.time()
    files = _collect_source_files()
    before = config.snapshot_md5(files)
    conn = db.open_db()
    budget = CallBudget(args.max_api_calls)

    print(paint("数据源：%s" % config.SRC_DIR, "dim"))
    print(paint("目标库：%s" % config.DB_PATH, "dim"))
    print()

    # 1) 解析 + 切分 + 抽字段
    t = time.time()
    parsed_docs, all_chunks, skipped = 0, [], []
    # 按"切片家族"顺序处理：后一份切片继承前一份结尾的板块。
    # 切片 _2/_3 常常从某个板块中间起头（第一段就是 Heading 3），
    # 不继承就会把这些单位/项目误判成抬头。
    for family in config.group_by_family(files):
        carried = None
        for path in family:
            name = os.path.basename(path)
            doc_id = db.doc_id_of(name)
            lang = config.lang_of(name)

            if not args.force and db.doc_md5(conn, doc_id) == before[path]:
                skipped.append(name)
                carried = db.last_section(conn, doc_id) or carried
                continue

            paras = docx_reader.read_docx(path)
            chunks = chunker.build_chunks(paras, doc_id, lang, start_section=carried)

            # 硬校验：子块并集必须逐字等于原文（零丢失、零重复）
            merged = chunker.union_text(chunks)
            assert merged == docx_reader.full_text(paras), \
                "%s 子块并集与原文不一致" % name
            for child in chunker.leaves(chunks):
                assert child.para_start < child.para_end, "%s 出现空区间子块" % name

            db.upsert_document(conn, {
                "doc_id": doc_id, "file_name": name, "lang": lang,
                "slice_no": config.slice_no_of(name), "md5": before[path],
                "para_count": len(paras),
            })
            db.replace_chunks(conn, doc_id, [c.as_row() for c in chunks])
            entries = extract_profile.extract_entries(chunks, doc_id, lang)
            db.replace_profile_entries(conn, doc_id, entries)
            conn.commit()

            parsed_docs += 1
            all_chunks.extend(chunks)
            carried = chunker.last_section(chunks) or carried
            print(paint("  * %-34s 段落%3d  父块%2d  子块%3d  字段%2d"
                        % (name, len(paras), len(chunker.parents(chunks)),
                           len(chunker.leaves(chunks)), len(entries)), "dim"))
    pruned = _prune_missing(conn, {os.path.basename(p) for p in files})
    conn.commit()
    _stage(1, 5, "解析 / 切分 / 抽字段", "%d 个文件（跳过 %d）"
           % (parsed_docs, len(skipped)), time.time() - t)

    # 2) 向量化
    t = time.time()
    report = _stage_embed(conn, budget, do_embed=not args.no_embed, force=args.force)
    if report["skipped"]:
        detail = "已跳过（%s）" % report.get("reason", "--no-embed")
    else:
        detail = "向量 %d 条（缓存命中 %d，本次调用 %d 次）" % (
            report["embedded"], report["cached"], report["api_calls"])
    _stage(2, 5, "向量化", detail, time.time() - t)

    # 3) 只读校验
    t = time.time()
    after = config.snapshot_md5(files)
    assert before == after, "源文件被改动！建库流程违反只读约定"
    _stage(3, 5, "源文件只读校验", "6 个文件 MD5 前后一致", time.time() - t)

    # 4) 派生索引：块 → 年份
    # 纯 SQL 推导，不联网、不改动任何 chunk 文本 —— 所以**已有的向量全部继续
    # 有效**，重建它零成本。正因为便宜，这里整表重建（而不是做增量），
    # 保证它与 profile_entries 永远不会脱节。
    t = time.time()
    year_rows = db.rebuild_chunk_years(conn)
    ystats = db.year_index_stats(conn)
    _stage(4, 5, "派生年份索引",
           "%d 条(块,年) 记录 / 覆盖 %d 个块 / 年份 %s~%s"
           % (year_rows, ystats["indexed_chunks"],
              ystats["min_year"] if ystats["min_year"] else "-",
              ystats["max_year"] if ystats["max_year"] else "-"),
           time.time() - t)

    # 5) 汇总
    t = time.time()
    info = db.stats(conn)
    _stage(5, 5, "写入 SQLite",
           "documents %d / chunks %d / embeddings %d / profile_entries %d / chunk_years %d"
           % (info["documents"], info["chunks"], info["embeddings"],
              info["profile_entries"], ystats["year_rows"]), time.time() - t)

    if pruned:
        print(paint("  已清理库内不存在于数据源的文档 %d 个" % pruned, "dim"))
    print()
    print(paint("完成，用时 %.2fs" % (time.time() - t0), "green"))
    print("库文件：%s" % config.DB_PATH)
    if report.get("skipped") and report.get("reason"):
        print(paint("提示：%s；配置好 Key 后重跑 build 即可补齐向量（已缓存的不重复消费额度）。"
                    % report["reason"], "yellow"))
    conn.close()
    return 0


# --------------------------------------------------------------- 检索

def _resolve_section(value):
    if not value:
        return None
    return config.SECTION_ALIASES.get(value.lower(), value)


def _print_hits(hits, query: str, elapsed: float):
    if not hits:
        print(paint("没有命中任何片段。", "yellow"))
        return
    print(paint("命中 %d 段（%s，用时 %.3fs）"
                % (len(hits), hits[0].source, elapsed), "dim"))
    print()
    for idx, hit in enumerate(hits, 1):
        cite = answer_mod.Citation(
            file_name=hit.file_name, section=hit.section, heading=hit.heading,
            para_range=hit.para_range)
        print("%s %s" % (paint("[%d]" % idx, "bold"), paint("%.4f" % hit.score, "blue")))
        print("    %s" % paint(cite.label(), "dim"))
        for line in hit.text.strip().split("\n")[:6]:
            print("    %s" % preview(line, 110))
        print()


def cmd_search(args) -> int:
    conn = db.open_db()
    budget = CallBudget(args.max_api_calls)
    query = _read_query(args)
    t0 = time.time()
    hits = retrieve.search(
        conn, query, mode=args.mode, lang=args.lang, section=_resolve_section(args.section),
        top_k=args.top, use_rerank=not args.no_rerank,
        embedder=get_embedder(budget=budget) if config.EMBED_API_KEY else None,
        reranker=get_reranker(budget=budget), budget=budget,
    )
    _print_hits(hits, query, time.time() - t0)
    conn.close()
    return 0 if hits else 1


def _read_query(args) -> str:
    if getattr(args, "query_file", None):
        with open(args.query_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    text = " ".join(getattr(args, "query", []) or []).strip()
    if text:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("请提供查询内容，例如：python -m pkdb.cli ask \"哪几个项目做过自动化测试？\"")


def cmd_ask(args) -> int:
    conn = db.open_db()
    budget = CallBudget(args.max_api_calls)
    query = _read_query(args)
    t0 = time.time()

    llm = get_llm(budget=budget) if config.LLM_API_KEY else None
    result = answer_mod.ask(
        conn, query, mode=args.mode, lang=args.lang,
        section=_resolve_section(args.section), top_k=args.top,
        use_rerank=not args.no_rerank,
        embedder=get_embedder(budget=budget) if config.EMBED_API_KEY else None,
        reranker=get_reranker(budget=budget), llm=llm, budget=budget,
    )

    print()
    print(paint("问：%s" % query, "bold"))
    print()
    print(result.text)
    print()

    if result.citations:
        print(paint("来源：", "bold"))
        for idx, cite in enumerate(result.citations, 1):
            print("  [%d] %s" % (idx, cite.label()))
    if result.degraded:
        print()
        print(paint("（本次为降级输出：未接入大模型或调用失败）", "yellow"))
    print()
    print(paint("用时 %.2fs   API 调用 %d 次" % (time.time() - t0, budget.used), "dim"))
    conn.close()
    return 0


# --------------------------------------------------------------- 统计

def cmd_stats(args) -> int:
    conn = db.open_db()
    info = db.stats(conn)
    print(paint("库文件：%s" % config.DB_PATH, "dim"))
    print("最近建库时间：%s" % (info["ingested_at"] or "--"))
    print()
    print("documents        %4d" % info["documents"])
    print("chunks           %4d  （其中子块 %d）" % (info["chunks"], info["leaf_chunks"]))
    print("embeddings       %4d" % info["embeddings"])
    print("profile_entries  %4d" % info["profile_entries"])
    print()
    if info["by_section"]:
        print(paint("按板块：", "bold"))
        for name, count in info["by_section"].items():
            print("  %-10s %4d" % (name, count))
    if info["by_kind"]:
        print()
        print(paint("结构化记录：", "bold"))
        for name, count in info["by_kind"].items():
            print("  %-10s %4d" % (name, count))
    conn.close()
    return 0


# --------------------------------------------------------------- 自检

def cmd_doctor(args) -> int:
    ok = True

    def line(label, good, detail):
        nonlocal ok
        mark = paint("OK  ", "green") if good else paint("FAIL", "red")
        if not good:
            ok = False
        print("%s %-22s %s" % (mark, label, detail))

    def report(label, good, message):
        """多行报告：首行进对齐列，后续行缩进显示（不要吞掉排错提示）。"""
        lines = [ln.strip() for ln in str(message).split("\n") if ln.strip()]
        line(label, good, lines[0] if lines else "")
        for extra in lines[1:]:
            print("       " + extra)

    files = config.source_docx_files()
    line("数据源目录", os.path.isdir(config.SRC_DIR), config.SRC_DIR)
    line("切片文件数", len(files) == config.EXPECTED_DOC_COUNT,
         "%d 个（预期 %d 个）" % (len(files), config.EXPECTED_DOC_COUNT))
    for path in files:
        print(paint("       - %s" % os.path.basename(path), "dim"))

    line("库文件", os.path.isfile(config.DB_PATH),
         config.DB_PATH if os.path.isfile(config.DB_PATH) else "尚未建库（先跑 build）")

    if os.path.isfile(config.DB_PATH):
        conn = db.open_db()
        info = db.stats(conn)
        line("库内文档", info["documents"] > 0, "%d 篇 / chunks %d"
             % (info["documents"], info["chunks"]))
        # 判定口径统一走 db.vector_coverage，别再各写一遍
        coverage = db.vector_coverage(conn)
        line("向量覆盖", coverage["ready"],
             "%d 条向量 / %d 个可向量化子块（另 %d 个为纯标题块，按设计跳过）"
             % (coverage["got"], coverage["need"], coverage["skipped"]))
        # 判定口径同样只算一处（db.year_index_stats），别在这里重算一遍
        ystats = db.year_index_stats(conn)
        line("年份索引", ystats["ready"],
             "%d 个块 / %d 条(块,年) 记录 / 年份 %s~%s"
             % (ystats["indexed_chunks"], ystats["year_rows"],
                ystats["min_year"] if ystats["min_year"] else "-",
                ystats["max_year"] if ystats["max_year"] else "-"))
        conn.close()

    print()
    line("embedding Key", bool(config.EMBED_API_KEY),
         "%s  %s" % (_mask(config.EMBED_API_KEY), config.EMBED_MODEL))
    line("对话模型 Key", bool(config.LLM_API_KEY),
         "%s  %s" % (_mask(config.LLM_API_KEY), config.LLM_MODEL))
    # 额外参数填错必须报出来：否则会出现"以为关了思考模式其实没关"的死角
    if config.LLM_EXTRA_BODY_ERROR:
        report("对话额外参数", False, config.LLM_EXTRA_BODY_ERROR)
    elif config.LLM_EXTRA_BODY:
        line("对话额外参数", True,
             json.dumps(config.LLM_EXTRA_BODY, ensure_ascii=False))
    if not config.RERANK_ENABLED:
        rerank_note = "已在配置中关闭（PKDB_RERANK_ENABLED=0）"
    elif config.RERANK_API_KEY:
        rerank_note = "已启用"
    else:
        rerank_note = "未配置 Key，重排会跳过（不影响检索）"
    line("rerank", config.RERANK_ENABLED and bool(config.RERANK_API_KEY),
         "%s  %s" % (config.RERANK_MODEL, rerank_note))

    if args.net:
        print()
        print(paint("连通性测试（会消耗极少免费额度）...", "dim"))
        budget = CallBudget(10)
        if config.EMBED_API_KEY:
            try:
                vec = get_embedder(budget=budget).embed(["ping"])[0]
                report("embedding 连通", True, "返回维度 %d" % len(vec))
            except Exception as exc:
                report("embedding 连通", False, exc)
        if config.LLM_API_KEY:
            try:
                reply = get_llm(budget=budget).chat("只回复 OK", "ping")
                report("对话模型连通", bool(reply), preview(reply, 40) or "(空回复)")
            except Exception as exc:
                report("对话模型连通", False, exc)

    print()
    print(paint("自检结论：%s" % ("全部通过" if ok else "存在未通过项，见上方 FAIL"), 
                "green" if ok else "yellow"))
    if not config.EMBED_API_KEY or not config.LLM_API_KEY:
        print(paint("提示：把 .env.example 复制为 .env 并填入 Key，即可解锁向量化与问答。", "dim"))
    return 0 if ok else 1


# --------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pkdb.cli",
        description="个人简历数据库 + RAG 检索原型（全本地存储，云端只做向量化与语言组织）",
    )
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细日志")
    parser.add_argument("--max-api-calls", type=int, default=config.MAX_API_CALLS_PER_RUN,
                        help="单次运行的 API 调用上限（保护免费额度）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="建库：解析 / 切分 / 抽字段 / 向量化")
    p_build.add_argument("--no-embed", action="store_true", help="只入库，不做向量化")
    p_build.add_argument("--force", action="store_true",
                         help="忽略 MD5 与缓存，强制重新解析与重新向量化")
    p_build.set_defaults(func=cmd_build)

    def add_query_args(sub_parser):
        sub_parser.add_argument("query", nargs="*", help="查询内容（也可用管道传入）")
        sub_parser.add_argument("--query-file", help="从 UTF-8 文本文件读取查询内容")
        sub_parser.add_argument("--mode", default="hybrid",
                                choices=["hybrid", "keyword", "semantic"],
                                help="hybrid=关键词+语义融合；keyword=只用 BM25；semantic=只用向量")
        sub_parser.add_argument("--lang", default=None, choices=["zh", "en", "all"],
                                help="限定语言")
        sub_parser.add_argument("--section", default=None,
                                help="限定板块：head/overview/strength/project/work/edu")
        sub_parser.add_argument("--top", type=int, default=config.TOP_K, help="返回条数")
        sub_parser.add_argument("--no-rerank", action="store_true", help="关闭重排")

    p_search = sub.add_parser("search", help="纯检索（不调大模型）")
    add_query_args(p_search)
    p_search.set_defaults(func=cmd_search)

    p_ask = sub.add_parser("ask", help="自然语言问答（检索 + 大模型组织语言）")
    add_query_args(p_ask)
    p_ask.set_defaults(func=cmd_ask)

    sub.add_parser("stats", help="查看库内统计").set_defaults(func=cmd_stats)

    p_doc = sub.add_parser("doctor", help="自检：路径 / Key / 连通性")
    p_doc.add_argument("--no-net", dest="net", action="store_false",
                       help="跳过联网测试")
    p_doc.set_defaults(func=cmd_doctor, net=True)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.no_color or os.environ.get("NO_COLOR"):
        _COLOR["enabled"] = False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
