# -*- coding: utf-8 -*-
"""docx 段落级读取。

复用工作区 extract_docx.py 的手法：直接读 zip 里的 word/document.xml。
差别在于这里保留 **段落顺序** 与 **标题层级**——分块要靠它识别板块边界。

返回的段落索引即 ``Document.paragraphs`` 的下标，与 split_docx.py 的切片口径一致，
因此 ``para_start/para_end`` 可以直接回指原文第几段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import docx
from docx.oxml.ns import qn

# 兼容英文样式名 "Heading 2" 与中文样式名 "标题 2"
_HEADING_RE = re.compile(r"(?:Heading|\u6807\u9898)\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class Para:
    """一个段落：序号 + 文本 + 标题层级（None 表示正文）。"""

    index: int
    text: str
    level: int = None
    style: str = ""


def _outline_level(paragraph):
    """从 w:pPr/w:outlineLvl 兜底判断大纲级别（outlineLvl 0 == 一级标题）。"""
    p_pr = paragraph._p.pPr
    if p_pr is None:
        return None
    node = p_pr.find(qn("w:outlineLvl"))
    if node is None:
        return None
    try:
        return int(node.get(qn("w:val"))) + 1
    except (TypeError, ValueError):
        return None


def _level_of(paragraph):
    """优先看样式名，其次看大纲级别。"""
    style_name = ""
    try:
        style_name = paragraph.style.name or ""
    except Exception:
        style_name = ""

    match = _HEADING_RE.search(style_name)
    if match:
        return int(match.group(1)), style_name

    level = _outline_level(paragraph)
    if level is not None:
        return level, style_name or "outline"

    return None, style_name


def read_docx(path: str):
    """读取 docx，返回 ``list[Para]``（保持文档顺序，含空段落）。"""
    document = docx.Document(path)
    paras = []
    for index, paragraph in enumerate(document.paragraphs):
        level, style_name = _level_of(paragraph)
        paras.append(
            Para(index=index, text=paragraph.text or "", level=level, style=style_name)
        )
    return paras


def full_text(paras) -> str:
    """全文口径：所有段落按原顺序用换行拼接。

    这是"零丢失 / 零重复"校验的标准答案——子块文本再拼接必须与它逐字相等。
    """
    return "\n".join(p.text for p in paras)


def preview(text: str, limit: int = 40) -> str:
    """日志用预览：只显示前 N 字，不打印简历全文。"""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "\u2026"
