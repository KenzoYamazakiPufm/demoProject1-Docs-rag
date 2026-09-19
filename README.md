# pkdb —— 个人简历数据库 + RAG 检索原型

把简历的 Word 切片文档变成**本地可检索、可精确筛选、可自然语言提问**的个人数据库。

三层结构：

| 层 | 做什么 | 在哪 |
|---|---|---|
| **结构化数据库** | 单位 / 起止时间 / 职位 / 技术栈 / 项目 / 教育 / 语言 | 本地 SQLite |
| **文档语义检索** | 关键词（BM25）+ 向量语义 + RRF 融合 + 可选重排 | 本地 numpy |
| **大模型组织语言** | 把召回的原文段落写成通顺回答，并标注出处 | 云端 API |

**原文始终留在本地**；只有"文字出去、数字回来"的那一步会经过网络。
不装 Docker，不装向量数据库，不引入 LangChain / LlamaIndex。

---

## 快速开始

### 1）装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2）建库（不需要任何 Key 也能先跑通）

```powershell
python -m pkdb.cli build --no-embed     # 只入库：解析 / 切分 / 抽字段
python -m pkdb.cli stats                # 看统计
python -m pkdb.cli search "自动化测试" --mode keyword
```

### 3）配 Key，补齐向量与问答

```powershell
Copy-Item .env.example .env
# 打开 .env，至少填 PKDB_EMBED_API_KEY（向量化），想问答再填 PKDB_LLM_API_KEY
python -m pkdb.cli build               # 只补没算过的向量，已缓存的不重复消费额度
python -m pkdb.cli ask "哪几个项目用过自动化测试框架？"
```

> 想把网页放到公网（免费）？见 **[DEPLOY.md](DEPLOY.md)** ——
> 已支持 Streamlit Community Cloud：索引库随仓库提供、云端只读、Secrets 自动桥接成环境变量。

### 4）网页界面

```powershell
python -m streamlit run pkdb/app.py
```

首次运行会问邮箱，**直接回车跳过**（留空 = 不订阅任何东西）；
想以后都不问，加 `--server.headless=true`。

浏览器打开 `http://localhost:8501` 即可查询与对话，答案与出处同屏。

> ⚠️ **改了 `pkdb/*.py` 之后必须重启 Streamlit 进程**（终端 `Ctrl+C`，再重新启动）。
> Streamlit 只会重新执行 `app.py` 本身，**不会重新导入它 import 的模块**——
> 运行中的进程里 `pkdb.db` 还是启动时那一份，新加的函数根本不存在。
> 典型报错：`AttributeError: module 'pkdb.db' has no attribute 'xxx'`。
> （命令行 `python -m pkdb.cli ...` 每次都是新进程，不受此影响。）

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `python -m pkdb.cli build` | 建库：解析 → 切分 → 抽字段 → 向量化 |
| `... build --no-embed` | 只入库，不做向量化（无 Key 也能跑） |
| `... build --force` | 忽略 MD5 与向量缓存，强制全部重算 |
| `... search "关键词"` | 纯检索，不调大模型，直接看命中片段 |
| `... ask "问题"` | 检索 + 大模型组织语言，附来源 |
| `... stats` | 查看库内统计（文档 / 块 / 向量 / 结构化记录） |
| `... doctor` | 自检：数据源、库、Key、连通性 |

常用参数：`--mode hybrid|keyword|semantic`、`--lang zh|en|all`、
`--section head|overview|strength|project|work|edu`、`--top N`、`--no-rerank`。

> **中文参数提示**：PowerShell 传中文有时会被编码搞乱，可以用
> `--query-file 问题.txt`，或改成从标准输入管道传入。

---

## 数据流

```
6 个切片 docx（只读）
   │ 读段落 + 标题层级
   ▼
段落 ──按 Heading 2/3 切分──▶ 父块（板块级）+ 子块（单位/项目级）
   │                              ↑ 子块区间严格划分原文，零丢失零重复
   ├── 正则抽字段 ──▶ profile_entries（单位/时间/技术栈…，可 SQL 精确筛）
   │
   ▼
SQLite（pkdb/data/pkdb.sqlite3）
   ├── documents    有哪些文件、MD5、段数
   ├── chunks       正文 + 出处标签（文件名/切片号/语言/板块/段落区间）
   ├── embeddings   向量（float32 BLOB）
   └── profile_entries
   │
   ▼ 提问时
结构化预过滤(SQL) ─┐
BM25（关键词）    ─┼─▶ RRF 融合 ─▶（可选 rerank）─▶ Top5 原文
向量余弦（语义）  ─┘                                  │
                                                     ▼
                              "指令 + 召回原文 + 问题" 拼成字符串 ─▶ 对话大模型
                                                     │
                                                     ▼
                                            回答 + [1][2] 出处角标
```

**要点**：向量只用于"找"，大模型只读**原文文字**——它从没见过向量。

---

## 免费方案与成本

| 用途 | 推荐 | 说明 |
|---|---|---|
| 向量化 | 硅基流动 `BAAI/bge-m3` | 免费、中英双语、1024 维 |
| 重排 | 硅基流动 `BAAI/bge-reranker-v2-m3` | 免费，可关闭 |
| 对话 ⭐ | 硅基流动 `Qwen/Qwen2.5-7B-Instruct` | **免费，且与上面共用同一把 Key** |
| 对话（备选） | 智谱 `glm-4-flash` 系列 | 官方标注长期免费，但需另注册一家 |
| 对话（备选） | DeepSeek `deepseek-flash` | 无免费额度，但约 **0.005 元/次**（空闲时段） |

> **已实测（2026-09-19）**：**一把硅基流动 Key 就能覆盖 embedding + rerank + 对话**，
> 不需要注册第二家平台。`.env` 里把三处 API Key 填成同一个值即可。

**免费额度保护**（内置，不需要额外配置）：

- 向量结果持久化到 `embeddings` 表，命中缓存绝不重复调用
- 批量请求（默认 32 条/次）+ 429/5xx 指数退避（1/2/4/8 秒）
- 单次运行调用次数上限（`PKDB_MAX_API_CALLS`，超限直接报错停下）
- 太短的块（只有板块标题）不送去向量化

> DeepSeek 的空闲时段 = **周末全天 + 工作日 9:00–12:00 / 14:00–18:00 之外**，价格是高峰的一半。
> 另外 DeepSeek **没有 embedding 接口**，向量化仍需硅基流动或本地模型。

### ⚠️ 实测踩坑：账户余额为 0 时，连"免费模型"也会被拒

2026-09-19 实测（账号已实名认证）：

```
GET  /v1/models            → 200（Key 有效，鉴权通过，返回 95 个模型）
POST /v1/embeddings        → 402 {"code":30001,"message":"...balance is insufficient"}
  BAAI/bge-m3 / bge-large-zh-v1.5 / bge-large-en-v1.5   全部 402
POST /v1/chat/completions  → 402
  Qwen/Qwen2.5-7B-Instruct / THUDM/GLM-4-9B-0414        全部 402
```

**结论**：不是 Key 问题、不是模型 ID 问题、也不是实名问题 ——
**是账户层面的**：余额为 0 时，硅基流动连官方标价 ¥0 的模型也一律拒绝。

**处理方式**（二选一）：

1. 到控制台「费用中心」确认余额 / 代金券状态（代金券有有效期，过期即视为 0），
   必要时充值（哪怕 ¥1）或联系客服确认免费策略
2. **改走本地 embedding**——就是下面那一节，**代码零改动**，而且从此不受任何平台政策影响

---

## 换厂商（不改代码）

所有供应商都走 OpenAI 兼容端点，改 `.env` 三行即可：

```ini
PKDB_LLM_BASE_URL=https://api.deepseek.com
PKDB_LLM_MODEL=deepseek-flash
PKDB_LLM_API_KEY=sk-xxxx
# 第四行只有 DeepSeek 需要，见下
PKDB_LLM_EXTRA_BODY={"thinking": {"type": "disabled"}}
```

### 第四行是干什么的

DeepSeek 官方文档写明：**思考模式默认打开，effort 默认为 `high`**。不关掉的话每次问答会先"想"一遍 —— 变慢、思考 token 按输出计费，而且**思考模式下 `temperature` 会被静默忽略**（传了不生效、也不报错）。

所以 `PKDB_LLM_EXTRA_BODY` 是一根"原样透传进请求体"的配置，用来表达 OpenAI 协议之外、各家厂商特有的开关。**换别家时把它清空即可**，代码一行不用动。填错不会让程序崩，但会在 `doctor` 里报出来，避免"以为关了其实没关"。

想全部本地化（数据不出本机）：

```ini
PKDB_EMBED_BASE_URL=http://localhost:11434/v1   # Ollama
PKDB_EMBED_MODEL=bge-m3
PKDB_EMBED_DIM=1024
```

---

## 数据源与只读约定

- 入库范围：`F:\WorkSpace\Task\AI\个人知识库\处理后数据_切片\` 下的 6 个切片 docx
- **源目录全程只读**：建库前后对 6 个文件做 MD5 校验并断言未变
- 索引产物写在 `pkdb/data/pkdb.sqlite3`，与原始资料物理隔离
- `原始数据\` 与 `Backup\` 不参与入库（它们的内容与切片重复，一起入库会造成重复召回）

### 切片间板块继承

切片 `_2` / `_3` 常常从某个板块**中间**切开（第一段就是 Heading 3，前面没有 Heading 2）。
`pkdb` 会把同一家族切片按编号排序，让后一份**继承前一份结尾的板块**，
否则这些单位/项目会被误判成「抬头」。

---

## 扩展新文档

1. 把新的 docx 放进数据源目录（或改 `pkdb/config.py` 里的 `SRC_DIR`）
2. 文件命名带 `_1 / _2 / _3` 后缀时，前缀相同的会被当成同一切片家族
3. 跑 `python -m pkdb.cli build`，只有新文件会被解析，已入库的按 MD5 跳过

---

## 测试

```powershell
python -m pytest tests -q
```

48 个用例，**全部离线**（向量相关用例注入假 embedder，因此没有 Key 也能跑）：

- `test_ingest.py` —— 子块并集逐字等于原文、区间严格划分、chunk_id 可复现、
  重复建库不翻倍、源文件 MD5 不变、结构化记录可溯源、切片间板块继承、
  两种日期写法都能解析、**向量缓存命中后不再重复调用 API**、
  **向量覆盖判定只算"可向量化子块"（忽略按设计跳过的纯标题块）**
- `test_retrieve.py` —— 分词、BM25 排序（含**小语料 IDF 退化**的回归用例）、
  向量余弦、RRF 奖励"两路共识"、结构化预过滤、语言过滤、无向量时关键词路仍可用
- `test_answer.py` —— 引用必附来源、无召回不生成也不调大模型、
  调用失败降级、提示词里只有被召回的内容
- `test_app.py` —— 用 Streamlit 官方 `AppTest` 真跑一遍页面脚本，
  确保网页界面无异常、控件齐全、**无空卡片**、**无结构化筛选区**，
  **且已就绪时不再误弹"向量未就绪"横幅**
- `test_config.py` —— 数据源目录可用环境变量覆盖（`PKDB_SRC_DIR`）、
  **Streamlit Cloud 的 Secrets 能桥接成环境变量**、且不覆盖真实环境变量

---

## 已知限制

- **切片即 chunk**：父块（板块级）可能很长，超过 `PKDB_PARENT_CONTEXT_MAX_CHARS`
  时提示词只带子块原文，不回溯父块
- **结构化抽取是规则驱动**：匹配不到就留空，绝不猜测；换一套格式差异很大的简历时，
  可能需要调整 `pkdb/config.py` 里的关键词表与 `pkdb/extract_profile.py` 的正则
- **向量检索是暴力扫描**：数据量到 10 万条以上、单次检索超过几十毫秒时，
  把 `pkdb/retrieve.py` 里的扫描换成 Chroma / sqlite-vec / pgvector 即可，其余代码不用动
- **rerank 只跑粗筛后的候选**：这是刻意的——成对精排精度更高但更慢，只能小范围用
- 免费政策随时会变，以各平台控制台实时价格页为准
- **网页界面的"检索命中 N 段"与"模型判断片段不足"是两个独立结论**：
  前者是本地检索的结果，后者是大模型读完片段后的判断。
  两者同时出现**不是 bug** —— 说明检索找到了片段，但片段里确实没有该问题的答案。
  这种情况请展开「来源引用」自行核对
- 结构化精确筛选（`retrieve.filter_entries`）目前在库层面提供，**未挂到网页界面**
- **改代码后要重启 Streamlit**（见上文「网页界面」一节的说明）

### 写网页界面时的一个坑（已踩过）

**不要**用 `st.markdown('<div class="pk-card">')` + `st.markdown('</div>')` 去"包住"控件。
Streamlit 把每个 `st.markdown` 当独立元素渲染，未闭合的 `<div>` 会被自动补全，
结果是页面上凭空多出一个**空卡片**（看起来像"输入框上方一个诡异的装饰框"）。

要卡片效果，就把整块 HTML 放在**同一次** `st.markdown` 里一次输出。
`tests/test_app.py::test_app_has_no_stray_empty_card` 会守住这条。

---

## 目录结构

```
pkdb/
  config.py           配置中心（路径 / 模型 / 参数，其它模块不得硬编码路径）
  docx_reader.py      docx → 段落 + 标题层级
  chunker.py          段落 → 父块 / 子块（子块区间严格划分原文）
  extract_profile.py  正则抽结构化字段
  schema.sql          建表语句
  db.py               SQLite 读写、向量编解码、幂等 upsert
  llm/
    base.py           统一 POST、退避重试、调用次数上限
    openai_compat.py  embedding / rerank / chat 三个客户端
  retrieve.py         三路检索 + RRF 融合 + 结构化筛选（库层面 filter_entries）
  answer.py           提示词编排、引用组装、降级输出
  cli.py              命令行
  app.py              Streamlit 网页
  data/pkdb.sqlite3   索引产物（与原始资料隔离）
tests/                48 个离线用例（含网页界面冒烟测试）
.streamlit/
  config.toml         Streamlit 配置（深色主题 + 隐藏 Deploy 按钮，不含密钥）
DEPLOY.md             免费部署到 Streamlit Community Cloud 的完整步骤
```
