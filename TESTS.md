# 测试说明（TESTS.md）

`pkdb` 的测试设计说明。目标读者是接手这个项目的人（也可能是几个月后的自己）。

```powershell
python -m pytest tests -q          # 全量：77 passed
python -m pytest tests/test_years.py -q   # 只跑某一个文件
```

---

## 一、三条设计原则

### 1. 全程离线：不联网、不需要 Key、不花钱

所有用例都用**替身**替换真实的网络调用，所以**断网、没配任何 API Key 也能跑全绿**。
这一点是刻意的 —— 否则测试会依赖网络状况和账户余额，等于没有测试。

| 替身 | 替换掉 | 位置 |
|---|---|---|
| `FakeEmbedder` | 向量化 API（按预设映射返回假向量） | `tests/helpers.py` |
| `FakeEmbedderClient` | 同上，但用于 `cli._stage_embed` 的调用计数 | `tests/test_ingest.py` 内定义 |
| `FakeLLM` | 对话模型 API（可正常回复，也可抛 429） | `tests/test_answer.py` 内定义 |
| `_Recorder` | 对话模型 API，同时**记录提示词**以便断言 | `tests/test_years.py` 内定义 |
| `AppTest` | 浏览器（Streamlit 官方，直接跑页面脚本、不起服务器） | `tests/test_app.py` |

### 2. 每个用例一个全新的库

`tests/conftest.py` 里的 `conn` 夹具基于 `tmp_path`，**每个用例拿到一个全新的临时 SQLite**，
用例之间零污染，可以任意顺序执行。

需要真实数据的用例（`test_ingest.py`）会**主动 `pytest.skip`** 而不是失败 ——
因为云端部署时根本没有 `F:\` 盘。

### 3. 测试对象是**不变量**和**已复现的故障**，不是"当前实现"

断言尽量写成"无论怎么改实现，这条都必须成立"，而不是"现在就长这样"。
每条回归用例都会在 docstring 里写明**它防的是哪个已经发生过的 bug**。

---

## 二、九个测试角度

### 角度 1 · 入库不变量（`test_ingest.py`，14 个）

整个项目**最硬的底线**，三条不变量缺一不可。

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_source_files_exist` | 数据源目录 | 恰好 6 个 `.docx`，且无 Word 临时文件（`~$`） |
| `test_leaf_union_equals_source` | 6 个真实 docx | 子块按序拼接**逐字等于**原文（零丢失、零重复） |
| `test_leaf_ranges_partition_document` | 同上 | 子块区间**首尾相接**、不重叠、不遗漏；父块完整包含子块 |
| `test_section_carried_across_slices` | 切片 `_2` / `_3` | 首个子块**不得**被误归「抬头」（板块继承） |
| `test_chunk_ids_are_deterministic` | 同一文件跑两次 | `chunk_id` **完全一致** ⇒ 库可重建 |
| `test_embedding_stage_persists_and_hits_cache` | 连续跑 3 轮并计数 | 第 1 轮调 N 次 → 第 2 轮 **0 次** → `--force` 才再调 N 次 |
| `test_rebuild_is_idempotent` | 跑第 2、第 3 遍 | 行数**不变**（走 replace 而非 append） |
| `test_source_files_not_modified` | 全部入库一遍 | 源文件 MD5 **前后一致**（只读约定） |
| `test_profile_entries_have_traceable_chunk` | 全部结构化记录 | 每条都能**回连到真实存在的 chunk** |
| `test_embeddable_skips_tiny_chunks` | 全部子块 | 低于 `EMBED_MIN_CHARS` 的**不送向量化** |
| `test_vector_coverage_ignores_skipped_tiny_chunks` | 1 个正常块 + 1 个纯标题块 | 分母只算**可向量化**的 ⇒ 判定为**就绪** |
| `test_vector_coverage_not_ready_before_embedding` | 还没算向量 | `ready=False` |
| `test_vector_coverage_empty_db` | 空库 | `{need:0, got:0, ready:False, skipped:0}` |
| `test_extract_period_handles_both_formats` | `2016.10 – 2022.12` / `Oct 2016 - Dec 2022` / `至 今` / `Present` | 两种写法都要解析出 `2016-10`、`2022-12` |

> **回归背景**：`vector_coverage` 的分母曾写成"全部子块"，导致纯标题块被算成"缺向量"，
> `doctor` 与网页**各写一遍、各错一遍**。现在统一走 `db.vector_coverage` ——
> 这也是项目约定"同一判定口径只能有一个真相源"的由来。

---

### 角度 2 · 派生索引与缓存（免费额度保护）

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_index_rebuild_is_idempotent` | 年份索引建两遍 | `chunk_years` 行数不变 |
| `test_index_is_derived_from_profile_entries` | 博彦 2013-10~2016-10、泰雷兹 2016-10~2022-12 | 展开成逐年；**2017 只属于泰雷兹** |
| `test_index_skips_entries_without_a_real_chunk` | 一条指向不存在块的记录 | 索引里**不留幽灵**（只索引真实存在的块） |

---

### 角度 3 · 检索算法，单元级（`test_retrieve.py`）

| 层 | 用例 | 输入 → 期望 |
|---|---|---|
| 分词 | `test_tokenize_filters_punctuation` | `"自动化测试，Selenium / Pytest。"` → 无标点残留 |
| BM25 | `test_bm25_ranks_relevant_first` | 3 段，问「自动化测试框架」→ **A 排第一** |
| BM25 | `test_bm25_works_on_tiny_corpus` | **只有 2 段**（IDF 会退化为 0）→ 仍必须召回 A，且**无关文档不得因基线分被召回** |
| BM25 | `test_bm25_empty_query_returns_empty` | 空问句 → `[]` |
| 向量 | `test_vector_rank_orders_by_cosine` | 正交向量 → 按余弦降序 |
| 向量 | `test_vector_rank_dimension_mismatch_is_safe` | 查询维度对不上 → 返回 `[]` 而**不崩** |
| RRF | `test_rrf_rewards_mutual_agreement` | A 两路都靠前、C 单路第一 → **A 胜**（奖励共识） |
| RRF | `test_rrf_respects_top_n` | `top_n=2` → 只返回 2 条 |
| RRF | `test_rrf_weight_count_mismatch_falls_back_to_equal` | 权重个数与榜单数对不上 → **退回全 1**，不静默错配 |
| 年份 | `test_years_of_period_expands_inclusive_range` | `2016-10 ~ 2022-12` → `[2016..2022]`（含两端） |
| 年份 | `test_years_of_period_handles_open_ended` | `至今` / `present` → 展开到给定年份 |
| 年份 | `test_years_of_period_refuses_to_guess` | 缺起点 / 缺终点 / 终点早于起点 / 跨度 >60 年 → **一律返回空** |
| 年份 | `test_years_in_query_extracts_and_dedups` | `"2018 到 2017"` → `[2017, 2018]`；无年份 → `[]` |
| 年份 | `test_years_in_query_respects_digit_boundaries` | `"20165"`、`"编号12017"` → **不得**抽成 2016 / 2017 |

> **回归背景**：`rank_bm25` 的 `BM25Okapi` 在小语料上 IDF 恰好为 0，会让关键词路**整体失效**。
> 本项目只有 30 多个子块，正是这种小语料，所以必须锁住。

---

### 角度 4 · `search` 入口集成

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_search_keyword_mode_needs_no_embedder` | `mode=keyword`，不传 embedder | 有结果，`source="bm25"` |
| `test_search_hybrid_merges_two_routes` | `mode=hybrid` + 假 embedder | 两路融合，`source="rrf"` |
| `test_search_structured_filter_narrows_candidates` | `section=重点项目` | 结果**只来自**该板块 |
| `test_search_language_filter` | `lang=en` / `lang=zh` | 各自只返回对应语言的块 |
| `test_search_without_vectors_still_works` | 库里没有向量 | 语义路**自动缺席**，关键词路仍可用 |
| `test_search_blank_query_returns_empty` | `"   "` | `[]` |
| `test_filter_entries_by_structured_fields` | kind / tech / 起止时间 / 标题模糊 | SQL 精确筛选取值边界正确 |
| `test_year_rank_respects_prefilter` | 年份路 + 提前被 section 过滤掉 | **不得绕过**预过滤 |

---

### 角度 5 · 防幻觉与出处（`test_answer.py`）—— 这是**产品承诺**，不是内部正确性

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_no_recall_returns_fixed_text_without_calling_llm` | 问「量子纠缠实验数据」 | 回固定话术，且**一次都不调大模型** |
| `test_prompt_contains_only_retrieved_chunks` | 库里有段无关内容（"年会司仪"） | 提示词里**一个字都不许出现** |
| `test_answer_always_carries_citations` | 正常提问 | 引用数与命中数**相等**，且含文件名/板块/段落区间 |
| `test_system_prompt_forbids_fabrication` | 读 `SYSTEM_PROMPT` | 必须含"禁止"，且拒答话术为**新话术**「简历里没有写到这部分」 |
| `test_no_answer_detection` | 各种拒答话术 | 能区分**「模型拒答」**与**「本地没召回」** |
| `test_answer_flags_no_answer_without_losing_hits` | 模型拒答 | 仍要**保留命中的片段与来源**供人核对 |
| `test_system_prompt_has_no_internal_labels` | 读 `SYSTEM_PROMPT` | **不得**再出现「参考片段」/《结构化任职记录》这类内部标签；拒答话术必须是新话术 |
| `test_user_prompt_has_no_internal_labels` | 组装提示词 | 材料标题已自然化为「他的简历内容」，**不再有**「参考片段」 |

---

### 角度 6 · 降级与容错

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_degraded_output_when_no_llm` | `llm=None` | `degraded=True`，但仍**带来源** |
| `test_llm_failure_degrades_instead_of_raising` | 假 LLM 抛 `429` | **降级而非崩溃**，`error` 里能看到 429 |
| `test_llm_success_marks_not_degraded` | 假 LLM 正常回复 | `degraded=False`，且**只调 1 次** |
| `test_chat_requires_api_key` | 没配 Key | 抛可操作的 `ApiError`（提示 `PKDB_LLM_API_KEY`），**不发必然 401 的请求** |
| `test_ask_without_llm_still_answers_a_year_question` | 没 Key 但问年份 | 降级路径也要能答对 |

---

### 角度 7 · 配置层与供应商中立（`test_config.py` / `test_llm.py`）

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_default_source_dir_points_to_local_disk` | — | 默认数据源是本地绝对路径 |
| `test_source_dir_env_override` | `PKDB_SRC_DIR=不存在` | `source_available()=False`，不报错（云端正常状态） |
| `test_source_available_reflects_real_dir` | 本机数据源 | 能列出全部 6 个切片 |
| `test_streamlit_secrets_bridge_imports_strings_only` | Secrets 里混有字符串/整数/字典 | **只有字符串**被桥接成环境变量 |
| `test_streamlit_secrets_bridge_never_overrides_real_env` | 环境变量与 Secrets 都有同键 | **环境变量优先** |
| `test_llm_extra_body_parsed_from_json` | 合法 JSON | 正确解析成 dict |
| `test_llm_extra_body_invalid_json_is_reported_not_swallowed` | `{thinking: disabled}` | **必须报错**，不能当成"没配" |
| `test_llm_extra_body_must_be_object` | `["thinking"]` | 同样报错 |
| `test_extra_body_lands_in_request_payload` | `{"thinking": {...}}` | **真的进了请求体**（否则等于没关思考模式） |
| `test_empty_extra_body_changes_nothing` | 不配置 | 请求体字段**一个都不多**，不污染别家厂商 |
| `test_extra_body_cannot_override_messages` | 故意传 `messages` | **提示词不得被顶掉** |
| `test_extra_body_can_override_sampling_params` | 传 `temperature` | 允许覆盖（给用户的逃生门） |

> **回归背景**：`test_extra_body_invalid_json_...` 防的是"以为关了思考模式、其实没关"——
> 这类**不报错的静默失败**是最难排查的一种，所以宁可让 `doctor` 直接 FAIL。

---

### 角度 8 · 界面冒烟（`test_app.py`，5 个）

用 Streamlit 官方 `AppTest` 真跑一遍 `app.py`，**不需要浏览器、不需要起服务器**。

| 用例 | 期望 |
|---|---|
| `test_app_renders_without_exception` | 脚本无异常，且渲染出内容 |
| `test_app_exposes_query_controls` | 输入框 / 模式切换 / 筛选下拉都在 |
| `test_app_shows_no_readiness_warning_when_ready` | 向量齐 + Key 配好时**不得再弹告警横幅** |
| `test_app_has_no_structured_filter_section` | 回归：结构化筛选区已按要求移除 |
| `test_app_has_no_stray_empty_card` | 回归：`pk-card` 必须是**整块自闭合 HTML** |

> **回归背景（空卡片）**：曾用 `st.markdown('<div class="pk-card">')` + `st.markdown('</div>')`
> 想"包住"控件，但 Streamlit 把每个 `st.markdown` 当独立元素、未闭合 div 被自动补全，
> 页面上凭空多出两个空卡片。**整块 HTML 必须在同一次 `st.markdown` 里输出。**

---

### 角度 9 · 端到端故障复现（`test_years.py`，20 个）

今天这个 bug 的**回归保护**。故障现象：问「2017在那个公司」，系统回
「参考片段中没有相关信息」，而检索却显示"命中 12 段"。

根因：简历只写区间 `2016.10 – 2022.12`，**正文里从不出现"2017"这三个字** ⇒
关键词路全灭、向量路把 2017 当成与 2016 最像的数字。

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_query_with_year_recalls_the_right_company` | 博彦 2013-10~2016-10、泰雷兹 2016-10~2022-12；问「2017在那个公司」 | **先断言前提**（关键词路确实一个字都找不到）→ 再断言 top1 = 泰雷兹、**博彦不得被召回** |
| `test_year_channel_must_outweigh_a_wrong_consensus` | 两路都把错块排第 1，年份路指向对块 | **不加权时确实会输** → 加权后**必须翻盘** |
| `test_query_without_year_is_unchanged` | 不含年份的问句 | 年份路**完全静默**，行为不回归 |
| `test_structured_evidence_computes_year_to_company` | 问「2017在那个公司」 | 证据文本含泰雷兹、含 `2016-10 ~ 2022-12`、**不含博彦** |
| `test_structured_evidence_is_silent_without_year` | 问「他做过自动化测试吗」 | 返回**空串** |
| `test_structured_evidence_refuses_when_nothing_covers` | 问「1998年他在哪家公司」 | 返回**空**，不给错的 |
| `test_structured_evidence_dedups_by_question_language` | 同一单位在 CN / EN 各存一条 | 问中文**只报中文那条**，不把两种语言报四遍 |
| `test_prompt_carries_structured_evidence_and_says_to_trust_it` | 组装提示词 | 必含自然化的「任职记录」表述与泰雷兹，且**不得**再出现《结构化任职记录》标签；`SYSTEM_PROMPT` 必含"必须直接采用" |
| `test_ask_passes_structured_evidence_to_the_model` | 假 LLM 记录提示词 | 算好的结论**真的进了提示词** |

### 关于 `test_year_channel_must_outweigh_a_wrong_consensus`

这条用例值得单独说 —— 它是**自证式**断言，不只测现状，还**证明设计决策的必要性**：

```python
# 真实情形：泰雷兹那段在问句上"一个字都不沾"，不会出现在 BM25 / 向量榜单里；
# 而错误的那段会同时占据两路第 1 名。不加权就是 1 票对 2 票，必输。
unweighted = retrieve.rrf_fuse([wrong, wrong, year], k=60)
assert unweighted[0][0] == "c_wrong", "无权重时确实会输 —— 正是要加权的理由"

weighted = retrieve.rrf_fuse([wrong, wrong, year], k=60,
                             weights=[1.0, 1.0, config.YEAR_CHANNEL_WEIGHT])
assert weighted[0][0] == "c_right", "加权后精确命中必须翻盘"
```

**它同时断言"不这么做就会错"。** 这样以后有人想把权重调回 1，测试会直接告诉他后果。

---

## 三、用例设计用了哪几种手法

| 手法 | 例子 |
|---|---|
| **等价类划分** | 中文 `2016.10 – 2022.12` / 英文 `Oct 2016 - Dec 2022`；`zh` / `en` 两种语言；`keyword` / `semantic` / `hybrid` 三种模式 |
| **边界值** | `EMBED_MIN_CHARS=10` 上下（4、6 字 vs ≥10 字）；年份数字边界 `20165`、`编号12017`；空查询 `"   "` |
| **异常 / 故障注入** | 缺 API Key、假 LLM 抛 429、向量维度不匹配、非法 JSON、指向不存在块的记录 |
| **幂等 / 重复执行** | 建库跑两遍；索引重建跑两遍；向量化跑三遍 |
| **只读校验** | 源文件 MD5 前后比对 |
| **不变量断言** | "子块并集 == 原文"这类无论怎么重构都必须成立的命题 |
| **自证式断言** | 同时断言"不这么做就会错"，把设计理由固化进测试 |
| **回归用例** | 每条都注明**防哪个已发生的 bug** |

---

## 四、诚实说：测不到的部分

| 测不到 | 原因 | 替代手段 |
|---|---|---|
| 真实 API 连通性 / Key 是否有效 | 测试不联网（刻意的） | 手工跑 `python -m pkdb.cli doctor` |
| **回答质量**（答得对不对） | 大模型输出无法逐字断言 | 只断言**可判定**的边界（拒答 / 不拒答、引用是否附来源）；内容质量靠人看 |
| 检索**语义**是否真的召回对的段 | 假向量按预设返回，不体现真实语义 | 手工跑 `python -m pkdb.cli search "..."` 看真实结果 |
| 网页美观度 / 交互手感 | `AppTest` 只测结构 | 人在浏览器里看 |
| 云端真实部署 | 无法模拟 Streamlit Cloud 环境 | `DEPLOY.md` 里有验收清单 |

> ⚠️ **今天那个 "2017" 的 bug 恰好落在第二行**：它答错了，但**全部测试都是绿的** ——
> 因为当时没有任何用例断言"该答对 X"。
>
> 这正是补 `test_query_with_year_recalls_the_right_company` 的原因：
> **把已经复现过的错误答案，变成一条可断言的回归用例。**
>
> 教训：**"测试全绿"只说明没踩到已经踩过的坑，不说明系统是对的。**
> 遇到一次线上/实际答错，第一件事就是把它固化成用例。

---

## 五、新增用例的约定

1. **优先放进已有文件**，只有出现新主题时才新建（如 `test_years.py` 对应年份路）。
2. **必须离线**。要调外部 API 就注入替身，不要真发请求。
3. **需要真实数据源的用例**，开头写 `if not source_files(): pytest.skip(...)`，
   不要假设 `F:\` 盘存在。
4. **回归用例**在 docstring 里写清**防的是什么 bug**、现象是什么。
5. **不要断言实现细节**（函数内部怎么分组、用什么数据结构），断言**可观测行为**。
6. 新增判定口径时，**先抽成 `db.py` 里的唯一函数**再写用例 ——
   同一个口径写在两处必然分叉（`vector_coverage` 已经踩过一次）。
