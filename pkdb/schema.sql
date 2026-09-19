-- pkdb 建表脚本
-- 双轨存储：关系表承载结构化字段（精确筛选），embeddings 表承载向量 BLOB（语义召回）。
-- 不引入向量数据库：数据量在千级以下时 numpy 暴力余弦检索毫秒级完成。

CREATE TABLE IF NOT EXISTS documents (
  doc_id      TEXT PRIMARY KEY,   -- 稳定标识：文件名的 sha1 前 12 位
  file_name   TEXT NOT NULL,      -- 简历_陈军_2026_CN_1.docx
  lang        TEXT NOT NULL,      -- zh | en
  slice_no    INTEGER,            -- 1 | 2 | 3
  md5         TEXT NOT NULL,      -- 源文件 MD5，未变则跳过重解析
  para_count  INTEGER,
  ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id   TEXT PRIMARY KEY,    -- sha1(doc_id + ':' + para_start + ':' + para_end)
  doc_id     TEXT NOT NULL,
  parent_id  TEXT,                -- 父块（板块级）chunk_id，实现 small-to-big
  section    TEXT NOT NULL,       -- 抬头|概述|核心优势|工作经历|重点项目|教育技能
  heading    TEXT,                -- 所属单位或项目标题
  lang       TEXT NOT NULL,
  para_start INTEGER NOT NULL,
  para_end   INTEGER NOT NULL,
  text       TEXT NOT NULL,
  n_chars    INTEGER
);

CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id   TEXT PRIMARY KEY,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vector     BLOB NOT NULL,       -- float32 小端序列，numpy frombuffer 直接还原
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_entries (
  entry_id  TEXT PRIMARY KEY,
  kind      TEXT NOT NULL,        -- company | project | education | skill | language
  title     TEXT,                 -- 单位名 / 项目名 / 学校
  role      TEXT,                 -- 职位
  start_ym  TEXT,                 -- YYYY-MM
  end_ym    TEXT,
  tech      TEXT,                 -- 逗号分隔技术栈
  chunk_id  TEXT,                 -- 回连原文，保证可溯源
  raw       TEXT
);

-- 派生索引：把「时间区间」展开成逐年，让"2017"这类**年份字面**能被精确召回。
--
-- 为什么必须有它：简历只用区间写法（``2016.10 – 2022.12``），正文里**从不出现
-- "2017"这三个字**。于是问"2017 在哪个公司"时——
--   关键词路（BM25）：字面不存在 → 全灭
--   向量路：把 2017 当成与 **2016 最像的数字** → 召回的全是 2016 年前后的块
-- 结果模型拿到"博彦科技（2013.10–2016.10）"这类片段，只能诚实回"片段里没有"。
-- 而"某年是否落在某个区间内"是**逻辑运算**，语义相似度天生做不了。
--
-- 本表**完全由 profile_entries 推导**：不读 docx、不调任何 API、不改动任何 chunk
-- 文本，所以已有的向量全部继续有效——重建它零成本，可以随便重跑。
CREATE TABLE IF NOT EXISTS chunk_years (
  chunk_id TEXT NOT NULL,           -- 覆盖了该年份的块（子块，检索只用子块）
  year     INTEGER NOT NULL,
  PRIMARY KEY (chunk_id, year)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc        ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent     ON chunks (parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_section    ON chunks (section);
CREATE INDEX IF NOT EXISTS idx_chunks_lang       ON chunks (lang);
CREATE INDEX IF NOT EXISTS idx_profile_kind      ON profile_entries (kind);
CREATE INDEX IF NOT EXISTS idx_profile_start_ym  ON profile_entries (start_ym);
CREATE INDEX IF NOT EXISTS idx_chunk_years_year   ON chunk_years (year);
