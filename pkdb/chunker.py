# -*- coding: utf-8 -*-
"""板块与标题驱动的分块（small-to-big）。

两层结构：

    父块 = 板块级（Heading 2）   —— 检索时不用，喂给大模型时作为上下文
    子块 = 单位 / 项目级（Heading 3）—— 检索的最小单位，保证精度

硬性约束：**同一父块内，子块的段落区间构成严格划分**——
首尾相接、不重叠、不遗漏。所以把所有子块文本按顺序拼起来，必然逐字等于原文。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .db import chunk_id_of

# 子块角色标记（参与 chunk_id 计算，避免与同区间的父块主键冲突）
ROLE_PARENT = "P"
ROLE_CHILD = "C"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    parent_id: str = None
    section: str = config.SECTION_HEAD
    heading: str = None
    lang: str = "zh"
    para_start: int = 0
    para_end: int = 0
    text: str = ""

    @property
    def n_chars(self) -> int:
        return len(self.text)

    @property
    def is_leaf(self) -> bool:
        return self.parent_id is not None

    def as_row(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "parent_id": self.parent_id,
            "section": self.section,
            "heading": self.heading,
            "lang": self.lang,
            "para_start": self.para_start,
            "para_end": self.para_end,
            "text": self.text,
            "n_chars": self.n_chars,
        }


def normalize_section(text: str):
    """把板块标题归一化到固定口径；识别不到返回 None（保持当前板块）。"""
    if not text:
        return None
    low = text.lower()
    for rule in config.SECTION_RULES:
        name, keywords = rule[0], rule[1:]
        for keyword in keywords:
            if keyword.lower() in low:
                return name
    return None


def _segments(values):
    """把相邻相同值的序列切成 [(start, end, value)]，覆盖 [0, len)。"""
    out = []
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            out.append((start, i, values[start]))
            start = i
    return out


def build_chunks(paras, doc_id: str, lang: str = "zh", start_section: str = None):
    """返回 ``list[Chunk]``：父块在前（文档序），子块在后（文档序）。

    ``start_section`` 是"起始板块"。切片文件常常从某个板块中间开始
    （第一段就是 Heading 3，前面没有 Heading 2），此时必须由调用方传入
    上一个切片结尾的板块，否则这些单位/项目会被误判成抬头。
    """
    if not paras:
        return []

    # 1) 逐段标注所属板块：遇到 Heading 1/2 就切板块
    section_of = []
    current = start_section or config.SECTION_HEAD
    for para in paras:
        if para.level is not None and para.level <= 2:
            mapped = normalize_section(para.text)
            if mapped:
                current = mapped
        section_of.append(current)

    parents, children = [], []

    # 2) 板块级父块 + 板块内的子块切分
    for start, end, section in _segments(section_of):
        parent_id = chunk_id_of(doc_id, start, end, ROLE_PARENT)

        head_text = None
        for i in range(start, end):
            if paras[i].level is not None and paras[i].level <= 2:
                head_text = paras[i].text
                break

        parents.append(
            Chunk(
                chunk_id=parent_id,
                doc_id=doc_id,
                parent_id=None,
                section=section,
                heading=head_text,
                lang=lang,
                para_start=start,
                para_end=end,
                text="\n".join(paras[i].text for i in range(start, end)),
            )
        )

        cursor = start
        heading = None
        for i in range(start, end):
            para = paras[i]
            if para.level is not None and para.level >= 3:
                if i > cursor:
                    children.append(
                        _make_child(doc_id, lang, section, heading, parent_id,
                                    paras, cursor, i)
                    )
                cursor = i
                heading = para.text
        children.append(
            _make_child(doc_id, lang, section, heading, parent_id, paras, cursor, end)
        )

    return parents + children


def _make_child(doc_id, lang, section, heading, parent_id, paras, start, end) -> Chunk:
    return Chunk(
        chunk_id=chunk_id_of(doc_id, start, end, ROLE_CHILD),
        doc_id=doc_id,
        parent_id=parent_id,
        section=section,
        heading=heading,
        lang=lang,
        para_start=start,
        para_end=end,
        text="\n".join(paras[i].text for i in range(start, end)),
    )


def leaves(chunks):
    """只取子块（检索的最小单位），保持文档顺序。"""
    return [c for c in chunks if c.is_leaf]


def parents(chunks):
    return [c for c in chunks if not c.is_leaf]


def last_section(chunks):
    """文档最后一段所属的板块（用于把上下文传给同家族的下一份切片）。"""
    blocks = parents(chunks)
    return blocks[-1].section if blocks else None


def embeddable(chunks, min_chars: int = None):
    """可送去向量化的子块（Chunk 对象）：非空、且长度达到阈值。"""
    floor = config.EMBED_MIN_CHARS if min_chars is None else min_chars
    return [c for c in chunks if c.is_leaf and len(c.text.strip()) >= floor]


def embeddable_rows(rows, min_chars: int = None):
    """同上，但输入是数据库行（建库流程里从库里取待向量化的块）。"""
    floor = config.EMBED_MIN_CHARS if min_chars is None else min_chars
    return [r for r in rows if len((r["text"] or "").strip()) >= floor]


def union_text(chunks) -> str:
    """子块按段落序拼接（用换行连接），用于"零丢失零重复"校验。"""
    ordered = sorted(leaves(chunks), key=lambda c: (c.para_start, c.para_end))
    return "\n".join(c.text for c in ordered)
