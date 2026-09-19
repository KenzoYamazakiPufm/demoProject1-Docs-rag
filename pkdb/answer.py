# -*- coding: utf-8 -*-
"""编排层：检索 -> 提示词 -> 调用大模型 -> 组装引用。

三条底线：
1. **无召回就不生成**：检索不到内容时直接回固定话术，绝不编造
2. **必有出处**：每条回答都带 Citation（文件 / 切片号 / 板块 / 段落区间）
3. **降级可用**：没有 Key 或调用失败时，退化为"最佳命中 + 模板化输出"，
   命令行与网页仍然能跑，方便先验证检索质量再决定要不要接大模型
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, db, retrieve
from .docx_reader import preview

# 有汉字就当中文，否则当英文。只用来在"结构化任职记录"里挑一种语言，
# 不做任何内容判断。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def guess_lang(text: str) -> str:
    return "zh" if _CJK_RE.search(text or "") else "en"

# 本地检索一段都没召回到时的固定话术（与"大模型拒答"是两回事）
NO_MATCH_TEXT = "未召回到相关片段。建议换个说法，或用 search 命令直接看命中情况。"

SYSTEM_PROMPT = (
    "你是一个个人简历问答助手。请遵守以下规则：\n"
    "1. 只能依据下面提供的《参考片段》与《结构化任职记录》作答；"
    "禁止使用它们之外的知识，禁止推测、禁止编造。\n"
    "2. 先尽力从材料里找线索：材料中写了的内容要如实、完整地回答，不要无谓地拒答。\n"
    "3. 每一条结论后面都要标注来源编号，如 [1][2]。\n"
    "4. 遇到涉及年份或时间段的问题：《结构化任职记录》是本地数据库按时间区间"
    "精确计算出来的结果，必须直接采用，不要自己再去片段里推算；"
    "引用它时把来源写成《结构化任职记录》，不要为了凑角标去引用不相关的片段。\n"
    "5. 只有在材料中确实找不到与该问题相关的任何信息时，才回复：参考片段中没有相关信息。"
)

# 判定"大模型是不是在拒答"用的标记。
# 命中检索、但模型认为片段不足以作答时，UI 需要把这两种情况区分开，
# 否则用户会看到"未找到相关内容"和"命中 3 段"并排出现，以为是 bug。
NO_ANSWER_MARKERS = (
    "未找到相关内容", "参考片段中没有相关信息", "没有相关信息",
    "无法回答", "未提及", "没有提及", "没有找到相关",
)


def looks_like_no_answer(text: str) -> bool:
    """模型是不是在说"片段里没有答案"（只看开头，避免把正常的限定语误判）。"""
    head = (text or "").strip()[:60]
    return bool(head) and any(marker in head for marker in NO_ANSWER_MARKERS)


# --------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class Citation:
    file_name: str
    slice_no: int = None
    section: str = ""
    heading: str = None
    para_range: str = ""

    def label(self) -> str:
        parts = [self.file_name]
        if self.slice_no is not None:
            parts.append("\u5207\u7247%d" % self.slice_no)
        if self.section:
            parts.append(self.section)
        if self.heading:
            parts.append(self.heading)
        if self.para_range:
            parts.append("\u7b2c%s\u6bb5" % self.para_range)
        return " \u00b7 ".join(parts)


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    degraded: bool = False
    query: str = ""
    error: str = None
    # 检索命中，但大模型判断片段不足以作答 —— 与"一段都没召回"完全不同
    no_answer: bool = False


# --------------------------------------------------------------- 上下文

def _doc_meta(conn) -> dict:
    """doc_id -> {file_name, slice_no}。转成普通 dict，避免 sqlite3.Row 没有 .get 的问题。"""
    return {
        row["doc_id"]: {"file_name": row["file_name"], "slice_no": row["slice_no"]}
        for row in conn.execute("SELECT doc_id, file_name, slice_no FROM documents")
    }


def _citations(conn, hits):
    meta = _doc_meta(conn)
    out = []
    for hit in hits:
        doc = meta.get(hit.doc_id, {})
        out.append(
            Citation(
                file_name=doc.get("file_name") or hit.file_name,
                slice_no=doc.get("slice_no"),
                section=hit.section,
                heading=hit.heading,
                para_range=hit.para_range,
            )
        )
    return out


def _context_text(conn, hit) -> str:
    """small-to-big：父块不长就带上整段（上下文完整），太长就只用子块（够精确）。"""
    row = db.get_chunk(conn, hit.chunk_id)
    if row is None or not row["parent_id"]:
        return hit.text
    parent = db.get_chunk(conn, row["parent_id"])
    if parent is None:
        return hit.text
    parent_text = parent["text"] or ""
    if 0 < len(parent_text) <= config.PARENT_CONTEXT_MAX_CHARS:
        return parent_text
    return hit.text


def structured_evidence(conn, question: str, lang: str = None) -> str:
    """问句里出现年份时，由本地数据库精确算出"该年属于哪些单位/项目"。

    **为什么必须由代码算死，而不是让模型从片段里推**：回答"2017 在哪个公司"
    需要判断 ``2016.10 <= 2017 <= 2022.12``，这是**逻辑运算**。实测（2026-09-19）
    把含正确区间的片段喂给 7B 模型，它仍然回"参考片段中没有相关信息"——
    检索修得再准也没用。所以这一步在代码里定死，模型只负责组织语言。

    问句里没有年份时返回空串，行为与改动前完全一致。
    """
    years = retrieve.years_in_query(question)
    if not years:
        return ""

    rows = db.entries_covering_years(conn, years, lang=lang or guess_lang(question))
    if not rows:
        return ""

    years_text = "\u3001".join(str(y) for y in years)
    lines = [
        "\u300a\u7ed3\u6784\u5316\u4efb\u804c\u8bb0\u5f55\u300b"
        "\uff08\u7531\u672c\u5730\u6570\u636e\u5e93\u6309\u65f6\u95f4\u533a\u95f4"
        "\u7cbe\u786e\u8ba1\u7b97\uff0c\u6d89\u53ca\u5e74\u4efd\u65f6\u5fc5\u987b"
        "\u76f4\u63a5\u91c7\u7528\uff09",
        "问题中的 " + years_text + " 年落在以下记录的时间范围内：",
    ]
    for row in rows:
        parts = [row["title"] or "-"]
        if row["role"]:
            parts.append(row["role"])
        parts.append("%s ~ %s" % (row["start_ym"] or "?", row["end_ym"] or "?"))
        lines.append("- " + " \u00b7 ".join(parts))
    return "\n".join(lines)


def build_user_prompt(conn, question: str, hits, citations, structured: str = "") -> str:
    """只拼接被召回的片段——未召回的内容绝不进提示词。"""
    blocks = []
    for idx, (hit, cite) in enumerate(zip(hits, citations), 1):
        lines = [
            "[%d] %s" % (idx, cite.label()),
            _context_text(conn, hit).strip(),
        ]
        blocks.append("\n".join(lines))

    parts = []
    if blocks:
        parts.append("\u300a\u53c2\u8003\u7247\u6bb5\u300b\n" + "\n\n".join(blocks))
    if structured:
        parts.append(structured)
    parts.append("\u300a\u95ee\u9898\u300b\n" + question.strip())
    return "\n\n".join(parts)


def _fallback_text(hits, citations, structured: str = "") -> str:
    """降级输出：结构化任职记录在前（那是确定答案），检索片段在后（供自己核对）。"""
    lines = []
    if structured:
        lines.append(structured)
        lines.append("")
    if hits:
        lines.append(
            "\u5c1a\u672a\u63a5\u5165\u5927\u6a21\u578b\uff08\u6216\u8c03\u7528\u5931\u8d25\uff09\uff0c"
            "\u4ee5\u4e0b\u662f\u68c0\u7d22\u5230\u7684\u6700\u76f8\u5173\u7247\u6bb5\uff1a"
        )
        lines.append("")
        for idx, (hit, cite) in enumerate(zip(hits, citations), 1):
            lines.append("[%d] %s" % (idx, cite.label()))
            lines.append("    " + preview(hit.text, config.FALLBACK_SNIPPET_CHARS))
            lines.append("")
    return "\n".join(lines).rstrip() or NO_MATCH_TEXT


# --------------------------------------------------------------- 主入口

def ask(conn, question: str, *, top_k: int = None, mode: str = "hybrid",
        lang: str = None, section: str = None, embedder=None, reranker=None,
        llm=None, budget=None, use_rerank: bool = None) -> Answer:
    hits = retrieve.search(
        conn, question, mode=mode, lang=lang, section=section, top_k=top_k,
        use_rerank=use_rerank, embedder=embedder, reranker=reranker, budget=budget,
    )
    # 结构化证据：问句里出现年份时，由**本地代码**精确算出"该年属于哪些单位/项目"。
    # 放在"无召回"判断之前是有意的：即使片段一段都没召回，只要结构化记录算得出来，
    # 也应该给出确定答案（这正是"个人数据库"相对"聊天机器人"的价值）。
    structured = structured_evidence(conn, question, lang=lang)

    if not hits and not structured:
        return Answer(text=NO_MATCH_TEXT, citations=[], hits=[], degraded=False,
                      query=question)

    citations = _citations(conn, hits)

    if llm is None or not getattr(llm, "api_key", None):
        return Answer(text=_fallback_text(hits, citations, structured),
                      citations=citations, hits=hits, degraded=True, query=question)

    prompt = build_user_prompt(conn, question, hits, citations, structured)
    try:
        text = llm.chat(SYSTEM_PROMPT, prompt)
    except Exception as exc:  # 网络 / 限流 / 鉴权失败都不该让整个程序崩
        return Answer(
            text=_fallback_text(hits, citations, structured)
            + "\n\n\u5df2\u964d\u7ea7\uff1a\u8c03\u7528\u5927\u6a21\u578b\u5931\u8d25\u3002\n"
            + "  %s" % exc,
            citations=citations, hits=hits, degraded=True, query=question,
            error=str(exc),
        )

    if not text:
        return Answer(text=_fallback_text(hits, citations, structured),
                      citations=citations, hits=hits, degraded=True, query=question)
    return Answer(text=text, citations=citations, hits=hits, degraded=False,
                  query=question, no_answer=looks_like_no_answer(text))
