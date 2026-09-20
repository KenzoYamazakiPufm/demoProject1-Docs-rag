# -*- coding: utf-8 -*-
"""pkdb 配置中心。

约定：
- 所有路径常量集中在本模块，其它模块不得硬编码路径。
- 中文路径一律用 ``\\uXXXX`` 转义硬编码（沿用 split_docx.py 的既有约定，
  避免在 shell 命令行里直接传中文路径导致乱码）。
- 供应商中立：``base_url`` + ``model`` + ``api_key`` 决定一切，全部可用 .env 覆盖，
  换厂商不需要改代码。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

# --------------------------------------------------------------------- 路径

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PKG_DIR)

DATA_DIR = os.path.join(PKG_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "pkdb.sqlite3")
SCHEMA_PATH = os.path.join(PKG_DIR, "schema.sql")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# 默认数据源（只读）：F:\WorkSpace\Task\AI\个人知识库\处理后数据_切片
# 可用环境变量 PKDB_SRC_DIR 覆盖。
# 云端部署时这个目录不存在也没关系——索引产物 pkdb/data/pkdb.sqlite3
# 会随仓库一起带上，云端只做只读查询，不需要重新建库。
_DEFAULT_SRC_DIR = os.path.join(
    "F:\\WorkSpace\\Task\\AI",
    "\u4e2a\u4eba\u77e5\u8bc6\u5e93",
    "\u5904\u7406\u540e\u6570\u636e_\u5207\u7247",
)

# 对照目录（只读参考，不参与入库）：F:\WorkSpace\Task\AI\个人知识库\原始数据
_DEFAULT_ORIGIN_DIR = os.path.join(
    "F:\\WorkSpace\\Task\\AI",
    "\u4e2a\u4eba\u77e5\u8bc6\u5e93",
    "\u539f\u59cb\u6570\u636e",
)

EXPECTED_DOC_COUNT = 6
TEXT_PREVIEW_CHARS = 40

# ------------------------------------------------------------- .env 读取


def _load_env(path: str = ENV_PATH) -> None:
    """读取 .env（存在才读）。优先用 python-dotenv，缺失时退回内置极简解析。

    真实环境变量优先，.env 只补缺，不覆盖。
    """
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except Exception:
        pass

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_json_object(name: str):
    """读取一个"JSON 对象"型环境变量，返回 ``(解析结果, 错误说明)``。

    用在"原样透传进请求体"这类配置上（厂商特性开关）。
    解析失败**不抛异常**——用户把配置写错不该让程序直接起不来；
    但也**绝不静默当成没配**——错误说明会带出去给 doctor 显示，
    否则"以为配了其实没生效"是最难排查的一种情况。
    """
    raw = _env(name)
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {}, "%s 不是合法 JSON：%s" % (name, exc)
    if not isinstance(parsed, dict):
        return {}, "%s 必须是 JSON 对象（形如 {\"a\": 1}），当前是 %s" % (
            name, type(parsed).__name__,
        )
    return parsed, ""


def _bridge_streamlit_secrets() -> None:
    """把 Streamlit Cloud 的 Secrets 桥接成环境变量。

    云端 Secrets 只暴露在 ``st.secrets`` 里，**不会自动进 os.environ**，
    而本项目所有配置都从环境变量读 —— 所以这里补一道桥。

    只在 streamlit **已经**被导入时执行（也就是"正在跑网页"），
    命令行 `python -m pkdb.cli` 不会因此变慢，也不会有额外依赖。
    """
    if "streamlit" not in sys.modules:
        return
    try:
        import streamlit as st  # type: ignore

        for key, value in dict(st.secrets).items():
            if isinstance(value, str) and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # 没有 secrets.toml / 读取失败：本地运行属正常情况，静默跳过
        pass


_load_env()
_bridge_streamlit_secrets()

# 数据源目录（可用环境变量覆盖；云端不存在也无妨）
SRC_DIR = _env("PKDB_SRC_DIR", _DEFAULT_SRC_DIR)
ORIGIN_DIR = _env("PKDB_ORIGIN_DIR", _DEFAULT_ORIGIN_DIR)

# ------------------------------------------------------- embedding（向量化）
# 注意计费陷阱：硅基流动上 `BAAI/bge-m3` 免费，`Pro/BAAI/bge-m3` 按量收费，
# 务必使用不带 Pro/ 前缀的模型 ID。
EMBED_BASE_URL = _env("PKDB_EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
EMBED_MODEL = _env("PKDB_EMBED_MODEL", "BAAI/bge-m3")
EMBED_API_KEY = _env("PKDB_EMBED_API_KEY") or _env("SILICONFLOW_API_KEY")
EMBED_DIM = _env_int("PKDB_EMBED_DIM", 1024)
EMBED_BATCH = _env_int("PKDB_EMBED_BATCH", 32)
# 太短的块（只有板块标题、或纯空行）不送去向量化，省免费额度。
EMBED_MIN_CHARS = _env_int("PKDB_EMBED_MIN_CHARS", 10)

# --------------------------------------------------------- rerank（重排，可关）
RERANK_ENABLED = _env("PKDB_RERANK_ENABLED", "1").lower() not in ("0", "false", "no")
RERANK_BASE_URL = _env("PKDB_RERANK_BASE_URL", "https://api.siliconflow.cn/v1")
RERANK_MODEL = _env("PKDB_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
RERANK_API_KEY = _env("PKDB_RERANK_API_KEY") or _env("SILICONFLOW_API_KEY")

# ------------------------------------------------------ LLM（对话 / 语言组织）
# 默认知谱 GLM 免费 Flash 系列。若控制台里模型 ID 不同（如 glm-4.7-flash），
# 改 .env 里的 PKDB_LLM_MODEL 即可；也可指向 DeepSeek：
#   PKDB_LLM_BASE_URL=https://api.deepseek.com
#   PKDB_LLM_MODEL=deepseek-flash
LLM_BASE_URL = _env("PKDB_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = _env("PKDB_LLM_MODEL", "glm-4-flash")
LLM_API_KEY = _env("PKDB_LLM_API_KEY") or _env("ZHIPU_API_KEY")
LLM_TIMEOUT = _env_int("PKDB_LLM_TIMEOUT", 60)
LLM_MAX_TOKENS = _env_int("PKDB_LLM_MAX_TOKENS", 1024)
LLM_TEMPERATURE = float(_env("PKDB_LLM_TEMPERATURE", "0.2") or 0.2)

# ------------------------------------ 对话请求体的额外字段（厂商特性开关）
# 有些厂商的开关不在 OpenAI 协议里，必须塞进请求体才生效。
# 最典型的例子：**DeepSeek 默认开启思考模式（effort=high）**，不改的话每次
# 问答都先"想"一遍 —— 明显变慢、思考 token 按输出计费，而且思考模式下
# temperature 会被静默忽略。关掉它：
#     PKDB_LLM_EXTRA_BODY={"thinking": {"type": "disabled"}}
#
# 这里刻意用"原样 JSON 透传"而不是给每家厂商写一个专用配置项，
# 是为了守住供应商中立：换厂商只改这一行的内容，代码一行不动。
# 填错不会让程序崩，但也**不会静默吞掉** —— doctor 会把它报出来，
# 避免出现"以为关了思考模式，其实根本没生效"这种最难查的情况。
#
# 写成"一次赋值"而不是先给初值再改，是因为全大写名字被视作常量：
# 重复赋值会被静态检查判为"重新定义常量"，也是真实的分叉隐患来源。
LLM_EXTRA_BODY, LLM_EXTRA_BODY_ERROR = _env_json_object("PKDB_LLM_EXTRA_BODY")

# ------------------------------------------------------------- 检索与融合参数
TOP_K = _env_int("PKDB_TOP_K", 5)
FUSION_TOP_K = _env_int("PKDB_FUSION_TOP_K", 20)
RRF_K = _env_int("PKDB_RRF_K", 60)

# ------------------------------------- 年份路（精确召回）在 RRF 融合里的权重
# 为什么必须 > 1：年份路来自结构化表的"区间包含"推导，是**精确**命中；
# 而 BM25 与向量都是**近似**命中。真实故障里两者会一起指错 ——
# 问"2017 在哪个公司"时，BM25 与向量双双指向 2016 年的"博彦科技"
# （因为"2017"最像"2016"），而正确答案"泰雷兹"在问句上**一个字都不沾**、
# 根本不会出现在那两路榜单里。于是成了 1 票对 2 票，不加权必输。
# 权重 3 的含义：**一个精确命中要能压过"两路近似命中都排第 1"**。
# 想回到"三路平等"就把这里设成 1。
YEAR_CHANNEL_WEIGHT = float(_env("PKDB_YEAR_WEIGHT", "3.0") or 3.0)

# small-to-big：喂给大模型时回溯父块的阈值。
# 父块（整个板块）可能很长，超出阈值就只用子块原文，避免提示词爆炸。
PARENT_CONTEXT_MAX_CHARS = _env_int("PKDB_PARENT_CONTEXT_MAX_CHARS", 600)

# 降级输出里每条片段截断长度
FALLBACK_SNIPPET_CHARS = _env_int("PKDB_FALLBACK_SNIPPET_CHARS", 200)

# ------------------------------------------------- 免费额度保护 / 重试退避
MAX_API_CALLS_PER_RUN = _env_int("PKDB_MAX_API_CALLS", 200)
RETRY_MAX = _env_int("PKDB_RETRY_MAX", 3)
RETRY_BACKOFF = (1, 2, 4, 8)

# ------------------------------------------------------------ 分块与板块口径
# 板块口径固定为 6 类；首个 Heading 2 之前的内容归入「抬头」。
SECTION_HEAD = "\u62ac\u5934"              # 抬头（姓名 / 职位 / 电话）
SECTION_OVERVIEW = "\u6982\u8ff0"          # 概述
SECTION_STRENGTH = "\u6838\u5fc3\u4f18\u52bf"  # 核心优势
SECTION_PROJECT = "\u91cd\u70b9\u9879\u76ee"   # 重点项目
SECTION_WORK = "\u5de5\u4f5c\u7ecf\u5386"     # 工作经历
SECTION_EDU = "\u6559\u80b2\u6280\u80fd"      # 教育 / 语言 / 技能

# 板块归一化规则：(归一化名称, 命中关键词...)，按先后顺序匹配，先命中者胜。
# 顺序有意为之：`重点项目` 必须排在 `工作经历` 之前，
# 否则 "PROJECT EXPERIENCE" 会被 "experience" 抢先归到工作经历。
SECTION_RULES = (
    (SECTION_OVERVIEW, "\u6982\u8ff0", "summary", "profile"),
    (SECTION_STRENGTH, "\u4f18\u52bf", "\u6838\u5fc3", "highlight", "strength"),
    (SECTION_PROJECT, "\u9879\u76ee", "project"),
    (SECTION_WORK, "\u5de5\u4f5c\u7ecf\u5386", "experience", "employment", "career"),
    (SECTION_EDU, "\u6559\u80b2", "\u8bed\u8a00", "\u6280\u80fd", "education", "skill", "language"),
)

# 板块的英文别名：命令行里传中文可能被 shell 编码搞乱，用别名更稳。
SECTION_ALIASES = {
    "head": SECTION_HEAD,
    "overview": SECTION_OVERVIEW,
    "strength": SECTION_STRENGTH,
    "project": SECTION_PROJECT,
    "work": SECTION_WORK,
    "edu": SECTION_EDU,
}

# 结构化抽取用的技术栈词表（大小写不敏感匹配）
TECH_KEYWORDS = (
    "Selenium", "Appium", "Pytest", "PyTest", "unittest", "TestNG", "JUnit",
    "Robot Framework", "JMeter", "LoadRunner", "Locust", "Postman", "SoapUI",
    "Fiddler", "Charles", "Allure", "Jenkins", "GitLab CI", "GitHub Actions",
    "CI/CD", "Docker", "Kubernetes", "K8s", "Linux", "Shell", "Bash",
    "Python", "Java", "JavaScript", "TypeScript", "SQL", "MySQL", "Oracle",
    "PostgreSQL", "Redis", "MongoDB", "Git", "SVN", "Jira", "Confluence",
    "TestRail", "ZenTao", "Power BI", "Tableau", "Excel", "VBA",
    "接口测试", "自动化测试", "性能测试", "兼容性测试", "回归测试",
    "UI自动化", "接口自动化", "测试框架", "测试用例",
)

# 单位 / 项目标题里的时间区间正则（匹配不到就留空，绝不猜）。
# 同时兼容三种写法：
#   2016.10 – 2022.12        （中文数字型）
#   2016-10 ~ 2022-12
#   Oct 2016 – Dec 2022      （英文月份名在年份之前，必须单独支持）
#   2023.01 – 至今 / Jan 2023 – Present
YM_RE = (
    r"(?:(?P<sm_name>[A-Za-z]{3,9})[\s,.]*)?"
    r"(?P<sy>\d{4})"
    r"(?:\s*[.\-/\u5e74]\s*(?P<sm_num>\d{1,2}))?"
    r"\s*(?:[-\u2013\u2014~\u81f3]|\bto\b)\s*"
    r"(?:(?:(?P<em_name>[A-Za-z]{3,9})[\s,.]*)?(?P<ey>\d{4})"
    r"(?:\s*[.\-/\u5e74]\s*(?P<em_num>\d{1,2}))?"
    r"|(?P<now>\u81f3\u4eca|present|now)\b)"
)

# 学历关键词（教育板块识别）
EDU_KEYWORDS = (
    "\u5927\u5b66", "\u5b66\u9662", "\u5b66\u6821", "\u672c\u79d1", "\u4e13\u79d1",
    "\u7855\u58eb", "\u535a\u58eb", "\u7814\u7a76\u751f",
    "university", "college", "institute", "bachelor", "master", "phd",
)

# 语言关键词
LANG_KEYWORDS = (
    "\u82f1\u8bed", "\u56db\u516d\u7ea7", "\u53e3\u8bed", "CET", "IELTS", "TOEFL",
    "\u65e5\u8bed", "english", "\u7ca4\u8bed",
)

# --------------------------------------------------------------- 工具函数


def md5_file(path: str) -> str:
    """计算文件 MD5（分块读，避免大文件占内存）。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def source_docx_files() -> list:
    """列出数据源目录下的切片 docx（排除 Word 临时文件），按文件名排序。"""
    if not os.path.isdir(SRC_DIR):
        return []
    names = [
        name
        for name in os.listdir(SRC_DIR)
        if name.lower().endswith(".docx") and not name.startswith("~$")
    ]
    return [os.path.join(SRC_DIR, name) for name in sorted(names)]


def source_available() -> bool:
    """数据源目录里有没有可用的切片 docx。

    云端部署时它不存在属于**正常状态**：索引库随仓库一起带上，只做只读查询。
    所以调用方需要据此判断"能否执行入库操作"，而不是直接抛错。
    """
    return bool(source_docx_files())


def snapshot_md5(paths=None) -> dict:
    """对（默认全部）数据源文件做一次 MD5 快照，用于建库前后只读校验。"""
    targets = source_docx_files() if paths is None else list(paths)
    return {path: md5_file(path) for path in targets}


def ensure_data_dir() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def slice_no_of(file_name: str):
    """从文件名尾部取出切片编号，如 ``简历_陈军_2026_CN_2.docx`` -> 2。"""
    stem = os.path.splitext(os.path.basename(file_name))[0]
    tail = stem.rsplit("_", 1)
    if len(tail) == 2 and tail[1].isdigit():
        return int(tail[1])
    return None


def family_of(file_name: str):
    """切片家族名：去掉尾部 ``_N``。

    同一个家族的切片是按顺序从一个文档切出来的，因此
    **后一个切片的开头要继承前一个切片的结尾板块**——否则以 Heading 3
    起头的切片（如从"工作经历"中间切开的那份）会丢掉板块归属。
    """
    stem = os.path.splitext(os.path.basename(file_name))[0]
    tail = stem.rsplit("_", 1)
    if len(tail) == 2 and tail[1].isdigit():
        return tail[0]
    return stem


def group_by_family(paths):
    """把切片文件按家族分组，组内按切片号升序。返回 ``list[list[path]]``。"""
    groups = {}
    for path in paths:
        groups.setdefault(family_of(path), []).append(path)
    return [
        sorted(groups[name], key=lambda p: slice_no_of(p) or 0)
        for name in sorted(groups)
    ]


def lang_of(file_name: str) -> str:
    """从文件名推断语言，识别不到时默认 zh。"""
    upper = os.path.basename(file_name).upper()
    if "_EN" in upper or upper.startswith("RESUME") or "_CN" not in upper and "EN" in upper:
        return "en"
    return "zh"
