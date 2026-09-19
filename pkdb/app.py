# -*- coding: utf-8 -*-
"""Streamlit 网页界面（本地单页应用）。

启动：
    python -m streamlit run pkdb/app.py

设计：深墨蓝底 + 玻璃拟态卡片 + 青碧色高亮；答案与出处同屏，可回溯原文。
没有 API Key 也能用——会自动降级为"最佳命中 + 模板化输出"。
"""

from __future__ import annotations

import os
import re
import sys
import time

# 让 `from pkdb import ...` 在任何启动方式下都能工作：
# 本地用 `python -m streamlit run` 时 CWD 已在 sys.path 里，
# 但 Streamlit Community Cloud 的启动器不保证这一点 —— 显式补上更稳。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st  # noqa: E402

from pkdb import answer as answer_mod  # noqa: E402
from pkdb import config, db, retrieve  # noqa: E402
from pkdb.llm import CallBudget, get_embedder, get_llm, get_reranker  # noqa: E402

CSS = """
<style>
:root {
  --bg0: #070d1a;
  --bg1: #0c1728;
  --card: rgba(255, 255, 255, 0.045);
  --line: rgba(255, 255, 255, 0.10);
  --ink: #e8eef7;
  --muted: #8fa3bf;
  --accent: #3ddad7;
  --accent-dim: rgba(61, 218, 215, 0.16);
  --warn: #f0b429;
}
.stApp {
  background:
    radial-gradient(1100px 600px at 12% -10%, #123049 0%, transparent 60%),
    radial-gradient(900px 520px at 100% 0%, #16324a 0%, transparent 55%),
    linear-gradient(160deg, var(--bg0), var(--bg1));
  color: var(--ink);
}
section.main > div { padding-top: 1.2rem; }
h1, h2, h3, h4 { color: var(--ink) !important; letter-spacing: .2px; }
.pk-top {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 16px 20px; margin-bottom: 14px;
  background: var(--card); border: 1px solid var(--line);
  border-radius: 16px; backdrop-filter: blur(10px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
}
.pk-title { font-size: 20px; font-weight: 700; }
.pk-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.pk-badges { display: flex; gap: 8px; flex-wrap: wrap; }
.pk-badge {
  font-size: 12px; color: var(--muted); padding: 4px 10px;
  border: 1px solid var(--line); border-radius: 999px;
  background: rgba(255,255,255,.03);
}
.pk-badge b { color: var(--accent); font-weight: 600; }
.pk-card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 16px; padding: 18px 20px; margin-bottom: 14px;
  backdrop-filter: blur(8px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 10px 30px rgba(0,0,0,.25);
  transition: border-color .18s ease, transform .18s ease;
}
.pk-card:hover { border-color: rgba(61,218,215,.34); }
.pk-answer { font-size: 15px; line-height: 1.8; white-space: pre-wrap; }
.pk-cite {
  color: var(--accent); font-size: 12px; font-weight: 600;
  padding: 0 2px;
}
.pk-src-label { color: var(--muted); font-size: 12px; }
.pk-hit {
  border-left: 2px solid var(--accent-dim); padding-left: 12px;
  margin: 10px 0; color: #cfdcec; font-size: 13px; line-height: 1.7;
}
mark {
  background: var(--accent-dim); color: var(--accent);
  padding: 0 2px; border-radius: 3px;
}
.stTextInput input, .stTextArea textarea {
  background: rgba(255,255,255,.04) !important;
  color: var(--ink) !important; border: 1px solid var(--line) !important;
  border-radius: 10px !important;
}
.stButton > button {
  border-radius: 10px; border: 1px solid var(--line);
  background: rgba(255,255,255,.05); color: var(--ink);
  transition: all .16s ease;
}
.stButton > button:hover { border-color: var(--accent); color: var(--accent); }
.stButton > button[kind="primary"] {
  background: linear-gradient(120deg, rgba(61,218,215,.22), rgba(61,218,215,.06));
  border-color: rgba(61,218,215,.5); color: var(--accent); font-weight: 600;
}
div[data-testid="stExpander"] {
  border: 1px solid var(--line); border-radius: 12px;
  background: rgba(255,255,255,.02);
}
</style>
"""


# --------------------------------------------------------------- 数据访问

@st.cache_resource(show_spinner=False)
def _connect(path: str):
    return db.open_db(path)


def _stats(conn):
    return db.stats(conn)


def _highlight(text: str, query: str) -> str:
    """把查询词在原文里高亮出来（纯前端展示，不影响检索）。"""
    escaped = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    terms = [t for t in set(retrieve.tokenize(query)) if len(t) >= 2]
    for term in sorted(terms, key=len, reverse=True):
        escaped = re.sub(
            "(%s)" % re.escape(term),
            r"<mark>\1</mark>",
            escaped,
            flags=re.IGNORECASE,
        )
    return escaped.replace("\n", "<br>")


def _rebuild():
    from pkdb import cli

    # 云端没有数据源，重建无从下手（正常情况下按钮也不会显示）
    if not config.source_available():
        st.warning("找不到数据源目录，无法重建索引。%s" % config.SRC_DIR)
        return 1
    try:
        return cli.main(["build"])
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:  # 重建失败不该把页面搞崩
        st.error("重建索引失败：%s" % exc)
        return 1


# --------------------------------------------------------------- 页面

def render():
    st.set_page_config(page_title="个人简历数据库 · RAG 检索", page_icon="🗂",
                       layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    if not os.path.isfile(config.DB_PATH):
        conn = None
        info = {"documents": 0, "chunks": 0, "leaf_chunks": 0, "embeddings": 0,
                "profile_entries": 0, "ingested_at": None}
        coverage = {"need": 0, "got": 0, "ready": False, "skipped": 0}
    else:
        conn = _connect(config.DB_PATH)
        info = _stats(conn)
        coverage = db.vector_coverage(conn)

    # 判定口径与 doctor 完全一致：分母是"可向量化子块"，不是全部子块
    embed_ready = coverage["ready"]
    llm_ready = bool(config.LLM_API_KEY)

    # ---------- 顶部栏
    left, right = st.columns([3, 2])
    with left:
        st.markdown(
            '<div class="pk-top"><div><div class="pk-title">个人简历数据库</div>'
            '<div class="pk-sub">结构化字段 + 文档语义检索 + 大模型组织语言</div></div>'
            '<div class="pk-badges">'
            '<span class="pk-badge">切片 <b>%d</b> 份</span>'
            '<span class="pk-badge">子块 <b>%d</b></span>'
            '<span class="pk-badge">字段 <b>%d</b></span>'
            '<span class="pk-badge">向量 <b>%s</b></span>'
            '<span class="pk-badge">模型 <b>%s</b></span>'
            '</div></div>'
            % (info["documents"], info["leaf_chunks"], info["profile_entries"],
               "就绪" if embed_ready else "未建",
               config.LLM_MODEL if llm_ready else "未配置"),
            unsafe_allow_html=True,
        )
    # 数据源可用才给"重建索引"入口：云端部署时数据源不存在是正常状态
    source_ready = config.source_available()
    with right:
        st.write("")
        col_a, col_b = st.columns([1, 1])
        with col_a:
            lang = st.selectbox("语言", ["全部", "中文", "英文"], index=0,
                                label_visibility="collapsed")
        with col_b:
            if source_ready:
                if st.button("重建索引", use_container_width=True):
                    with st.spinner("正在解析 / 切分 / 向量化…"):
                        _rebuild()
                    _connect.clear()
                    st.rerun()
            else:
                st.markdown(
                    '<div class="pk-badge">只读模式 · 索引随仓库提供</div>',
                    unsafe_allow_html=True)

    lang_map = {"全部": None, "中文": "zh", "英文": "en"}

    if not embed_ready or not llm_ready:
        missing = []
        if not embed_ready:
            if config.EMBED_API_KEY:
                # Key 是好的，只是向量还没算完 —— 别让人再去翻 .env
                missing.append(
                    "向量未就绪（语义检索不可用，检索会退化为关键词）"
                    "　→　在终端执行 `python -m pkdb.cli build` 补齐向量即可")
            else:
                missing.append(
                    "未配置向量化 Key（语义检索不可用，检索会退化为关键词）"
                    "　→　把 `.env.example` 复制为 `.env` 并填好 Key，再执行 build")
        if not llm_ready:
            missing.append(
                "未配置对话模型 Key（问答会退化为片段罗列）")
        st.info(" · ".join(missing))

    # ---------- 查询区
    # ⚠️ 不要用 st.markdown('<div class="pk-card">') + st.markdown('</div>') 去"包住"控件：
    # Streamlit 把每个 st.markdown 当独立元素渲染，未闭合的 div 会被自动补全，
    # 结果是页面上凭空多出一个空卡片（看起来像诡异的"装饰框"）。
    # 要卡片效果，就把整块 HTML 放在**同一次** st.markdown 里（见下方回答卡片）。
    question = st.text_input(
        "问题",
        placeholder="用一句话问，例如：哪几个项目用过自动化测试框架？",
        label_visibility="collapsed",
    )
    c1, c2, c3, c4 = st.columns([1.1, 1.1, 1, 1])
    with c1:
        mode_label = st.radio("模式", ["混合", "关键词", "语义"], horizontal=True)
    with c2:
        section_label = st.selectbox(
            "板块", ["全部", "工作经历", "重点项目", "教育技能", "概述", "核心优势", "抬头"])
    with c3:
        top_k = st.slider("返回条数", 1, 12, config.TOP_K)
    with c4:
        st.write("")
        go = st.button("查询", type="primary", use_container_width=True)

    mode_map = {"混合": "hybrid", "关键词": "keyword", "语义": "semantic"}
    section_map = {"全部": None, "工作经历": config.SECTION_WORK,
                   "重点项目": config.SECTION_PROJECT, "教育技能": config.SECTION_EDU,
                   "概述": config.SECTION_OVERVIEW, "核心优势": config.SECTION_STRENGTH,
                   "抬头": config.SECTION_HEAD}

    if not conn:
        st.warning("还没有建库。请在命令行执行：python -m pkdb.cli build")
        return

    if not question.strip():
        st.markdown(
            '<div class="pk-card"><div class="pk-src-label">'
            '空状态：输入一句话开始提问。可以试试「他做过哪些自动化测试的工作？」'
            '「2020 年以前在哪家单位？」「英语水平如何？」'
            '</div></div>', unsafe_allow_html=True)
        return

    if go or question:
        budget = CallBudget()
        started = time.time()
        with st.spinner("检索中…"):
            result = answer_mod.ask(
                conn, question,
                mode=mode_map[mode_label],
                lang=lang_map[lang],
                section=section_map[section_label],
                top_k=top_k,
                embedder=get_embedder(budget=budget) if config.EMBED_API_KEY else None,
                reranker=get_reranker(budget=budget),
                llm=get_llm(budget=budget) if config.LLM_API_KEY else None,
                budget=budget,
            )
        elapsed = time.time() - started

        # ---------- 回答卡片
        # 整块 HTML 一次输出，才是真正的一个卡片（拆成两次 markdown 就会变成空框）
        if result.hits:
            body = re.sub(r"\[(\d+)\]", r'<span class="pk-cite">[\1]</span>',
                          result.text.replace("&", "&amp;").replace("<", "&lt;"))
            body = body.replace("\n", "<br>")
        else:
            body = result.text

        meta = "检索命中 %d 段 · 用时 %.2fs · API 调用 %d 次" % (
            len(result.hits), elapsed, budget.used)
        if result.degraded:
            meta += " · 已降级输出"
        if result.no_answer:
            meta += " · 模型判断片段不足"

        st.markdown(
            '<div class="pk-card"><div class="pk-answer">%s</div>'
            '<div class="pk-src-label">%s</div></div>' % (body, meta),
            unsafe_allow_html=True,
        )

        if result.error:
            st.error("调用大模型失败：%s" % result.error)

        if not result.hits:
            st.warning("没有召回到任何片段。换个说法、切换模式（试试「关键词」），"
                       "或去掉板块限制再试一次。")
        elif result.no_answer:
            st.info(
                "**检索本身是成功的**（见上方「检索命中 N 段」），"
                "但大模型判断这些片段里没有该问题的答案 —— 这通常意味着简历里确实没写这件事。\n\n"
                "请展开下方「来源引用」自己核对一眼；如果内容其实是有的，换个说法再问一次即可。")

        # ---------- 来源引用区
        if result.citations:
            st.markdown("#### 来源引用")
            for index, (hit, cite) in enumerate(zip(result.hits, result.citations), 1):
                title = "[%d] %s · %.4f" % (index, cite.label(), hit.score)
                with st.expander(title, expanded=index <= 2):
                    st.markdown(
                        '<div class="pk-hit">%s</div>' % _highlight(hit.text, question),
                        unsafe_allow_html=True,
                    )


render()
