# -*- coding: utf-8 -*-
"""结构化字段抽取（单位 / 项目 / 教育 / 语言 / 技能 / 技术栈）。

以规则 + 正则为主，理由：
- 简历结构规整（单位行就是"名称 +（起止时间）"），正则足够
- 规则**免费、确定、可复现**，不占用免费额度，也不会同一份文件两次抽出不同结果
- 匹配不到一律留空，**绝不猜测**
- 每条记录都回连 chunk_id，保证可以点回原文

本模块全程离线，不调用任何大模型。
"""

from __future__ import annotations

import hashlib
import re

from . import config

KIND_COMPANY = "company"
KIND_PROJECT = "project"
KIND_EDUCATION = "education"
KIND_SKILL = "skill"
KIND_LANGUAGE = "language"

_YM_RE = re.compile(config.YM_RE, re.IGNORECASE)
_HEAD_NOISE_RE = re.compile(r"^\s*(?:\d+\s*[.、)]\s*)|[\s\-~\u2013\u2014|·•/\\]+$")

# 职位关键词（长词优先，避免"高级测试工程师"被"测试"抢先命中）
ROLE_KEYWORDS = (
    "\u6d4b\u8bd5\u5f00\u53d1\u5de5\u7a0b\u5e08",
    "\u9ad8\u7ea7\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u8d44\u6df1\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u81ea\u52a8\u5316\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u6027\u80fd\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u8f6f\u4ef6\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u6d4b\u8bd5\u5f00\u53d1",
    "\u6d4b\u8bd5\u5de5\u7a0b\u5e08",
    "\u6d4b\u8bd5\u7ecf\u7406",
    "\u6d4b\u8bd5\u4e3b\u7ba1",
    "\u6d4b\u8bd5\u7ec4\u957f",
    "\u8f6f\u4ef6\u6d4b\u8bd5",
    "QA Engineer",
    "Test Engineer",
    "SDET",
    "QA",
)

_RAW_PREVIEW = 200


# --------------------------------------------------------------- 小工具

def _entry_id(doc_id: str, kind: str, title: str, chunk_id: str) -> str:
    raw = "%s|%s|%s|%s" % (doc_id, kind, title or "", chunk_id or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _lines(text: str):
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


# 英文月份名 -> 数字（取前三位匹配，兼容 Jan / January / JANUARY）
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_from_name(name):
    if not name:
        return None
    return _MONTH_NUM.get(name[:3].lower())


def _norm_ym(year, month):
    if not year:
        return None
    if month:
        try:
            num = int(month)
        except (TypeError, ValueError):
            num = _month_from_name(str(month))
        if num and 1 <= num <= 12:
            return "%04d-%02d" % (int(year), num)
    return "%04d" % int(year)


def parse_period(text: str, lang: str = "zh"):
    """从文字里解析起止年月 -> (start_ym, end_ym)；解析不到返回 (None, None)。

    兼容 ``2016.10 – 2022.12`` 与 ``Oct 2016 – Dec 2022`` 两种写法。
    """
    if not text:
        return None, None
    match = _YM_RE.search(text)
    if not match:
        return None, None

    start = _norm_ym(match.group("sy"), match.group("sm_num") or match.group("sm_name"))
    if match.group("now"):
        end = "\u81f3\u4eca" if lang == "zh" else "present"
    else:
        end = _norm_ym(match.group("ey"), match.group("em_num") or match.group("em_name"))
    return start, end


def clean_title(heading: str):
    """去掉标题里的时间区间与列表编号，留下名称本身（括号内容保留）。"""
    if not heading:
        return None
    text = _YM_RE.sub(" ", heading)
    text = _HEAD_NOISE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or None


def extract_tech(text: str):
    """按词表扫出技术栈（大小写不敏感去重）。"""
    if not text:
        return None
    low = text.lower()
    found, seen = [], set()
    for keyword in config.TECH_KEYWORDS:
        key = keyword.lower()
        if key in seen:
            continue
        if key in low:
            seen.add(key)
            found.append(keyword)
    return ",".join(found) if found else None


def _detect_role(text: str):
    if not text:
        return None
    low = text.lower()
    for keyword in ROLE_KEYWORDS:
        if keyword.lower() in low:
            return keyword
    return None


def _row(doc_id, kind, title, chunk, role=None, start_ym=None, end_ym=None,
         tech=None, raw=None):
    return {
        "entry_id": _entry_id(doc_id, kind, title or "", chunk.chunk_id),
        "kind": kind,
        "title": title,
        "role": role,
        "start_ym": start_ym,
        "end_ym": end_ym,
        "tech": tech,
        "chunk_id": chunk.chunk_id,
        "raw": (raw if raw is not None else chunk.text)[:_RAW_PREVIEW],
    }


# --------------------------------------------------------------- 主流程

def extract_entries(chunks, doc_id: str, lang: str = "zh"):
    """输入某文档的全部 chunk，输出 profile_entries 行列表（已按 entry_id 去重）。"""
    rows = []
    for chunk in chunks:
        if not chunk.is_leaf:
            continue

        # 有 Heading 3 的子块 = 一个工作单位 / 一个项目
        if chunk.heading and chunk.section in (config.SECTION_WORK, config.SECTION_PROJECT):
            kind = KIND_COMPANY if chunk.section == config.SECTION_WORK else KIND_PROJECT
            title = clean_title(chunk.heading)
            body_head = "\n".join(_lines(chunk.text)[:3])
            start_ym, end_ym = parse_period(chunk.heading, lang)
            if not start_ym:
                start_ym, end_ym = parse_period(body_head, lang)
            role = _detect_role(chunk.heading) if kind == KIND_COMPANY else None
            if kind == KIND_COMPANY and not role:
                role = _detect_role(body_head)
            rows.append(
                _row(doc_id, kind, title, chunk, role=role,
                     start_ym=start_ym, end_ym=end_ym,
                     tech=extract_tech(chunk.text))
            )
            continue

        # 教育 / 语言 / 技能：按行识别
        if chunk.section == config.SECTION_EDU:
            for line in _lines(chunk.text):
                low = line.lower()
                if any(keyword.lower() in low for keyword in config.EDU_KEYWORDS):
                    rows.append(_row(doc_id, KIND_EDUCATION, line, chunk, raw=line))
                elif any(keyword.lower() in low for keyword in config.LANG_KEYWORDS):
                    rows.append(_row(doc_id, KIND_LANGUAGE, line, chunk, raw=line))
                else:
                    tech = extract_tech(line)
                    if tech and len(line) <= 160:
                        rows.append(
                            _row(doc_id, KIND_SKILL, line, chunk, tech=tech, raw=line)
                        )

    # 去重（同一行可能在一段里重复出现），保持首次出现顺序
    deduped, seen = [], set()
    for row in rows:
        if row["entry_id"] in seen:
            continue
        seen.add(row["entry_id"])
        deduped.append(row)
    return deduped
