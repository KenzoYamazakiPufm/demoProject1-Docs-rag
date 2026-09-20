# 部署到 Streamlit Community Cloud（免费 · 公开访问）

## 为什么这个项目好部署

- 索引库 `pkdb/data/pkdb.sqlite3` 只有约 **272 KB**，随仓库一起推上去即可
- 文档向量**已经算好存在库里**，云端不需要数据源、不需要重新建库
- 云端只做**只读查询**（网页上的「重建索引」按钮会自动隐藏）

> ⚠️ **纠正一个容易搞错的点**：云端仍然需要 embedding Key。
> 因为**你的"问题"也要被向量化**才能做语义检索 ——
> 文档向量是预存的，**查询向量要现算**。只是调用量极小（每次提问 1 次）。

---

## ⚠️ 上云前必读：这是简历

公开部署 = **任何人拿到链接都能看到简历内容**（姓名、电话、单位、任职时间）。
Streamlit Cloud **没有内置登录**，本项目按你的选择**未加密码门**。

建议：
- App URL 起一个**不容易被猜到**的名字（形如 `https://<你的应用名>.streamlit.app`）
- **不要**把链接发到公开场合
- 想收回：在 Streamlit Cloud 上删除该应用即可

---

## 前置条件

1. GitHub 账号
2. **代码已推送到 GitHub** —— 本仓库：`KenzoYamazakiPufm/demoProject1-Docs-rag`（**私有**）
   - 本机凭据已就绪：`gh auth login` + `gh auth setup-git`
   - ⚠️ 提交必须用 **noreply 邮箱**，否则推送会被 GitHub 拒（GH007），见文末「已知坑」
3. ⚠️ **私有仓库必须先授权**：用 GitHub 登录 https://share.streamlit.io 时，
   GitHub 会询问授权范围 —— 选 **Only select repositories**，并勾上 `demoProject1-Docs-rag`。
   **没授权的话，创建应用时仓库下拉框里根本看不到它**（这是私有仓库最容易卡住的一步）。
   事后要改范围：GitHub → Settings → Applications → Authorized OAuth Apps → Streamlit

---

## 第 1 步：确认两个文件的状态（一进一出）

```powershell
git check-ignore -v .env
# 期望有输出，说明 .env 被忽略：
# .gitignore:4:.env	.env
```

**必须忽略**：`.env` —— 里面是真实可用的 API Key，一旦提交就永久留在 git 历史里。

```powershell
git check-ignore -v pkdb/data/pkdb.sqlite3
# 期望【没有】输出（返回码 1）
```

**必须提交**：`pkdb/data/pkdb.sqlite3` —— 云端就靠它提供数据。
如果这行有输出（说明被忽略了），去掉 `.gitignore` 里对它的忽略规则再继续。

---

## 第 2 步：推送代码

```powershell
git add .
git status --short          # 再扫一眼，确认待提交列表里没有 .env
git commit -m "pkdb: 个人简历数据库 + RAG 检索原型"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

---

## 第 3 步：创建应用

1. 打开 https://share.streamlit.io → 用 GitHub 登录（**记得授权私有仓库**，见前置条件 3）
2. 点 **Create app** → **Deploy a public app from GitHub**
3. 填写：
   - Repository：**`KenzoYamazakiPufm/demoProject1-Docs-rag`**（私有仓库会带一个锁图标 🔒）
   - Branch：**`main`**
   - **Main file path：`pkdb/app.py`** ← 在子目录里，**最容易填错的一项**
   - App URL：自定一个**不好猜**的名字
4. 展开 **Advanced settings**：
   - Python version 选 **3.11**（与本地一致）
   - **Secrets** 粘贴第 4 步的内容
5. 点 **Deploy**，等 2–5 分钟

### ⚠️ "私有仓库" ≠ "私有应用"（最容易误解的一点）

部署出来的应用**默认是公开的** —— 任何人拿到 `https://<app-name>.streamlit.app` 都能打开。

**但 Streamlit Cloud 是有访问控制的**（本文件早先写"没有内置登录"是错的）：
部署完成后进入该应用的 **Settings**，其中有**分享 / 可见性（Sharing）**相关设置，
可以把可见范围收窄为「只有自己」或指定邮箱白名单。

---

## 第 4 步：配置 Secrets

云端 Secrets 只暴露在 `st.secrets` 里，**不会自动变成环境变量**。
`pkdb/config.py` 里已经补了一道桥（`_bridge_streamlit_secrets`），
所以按下面这样写，程序就能读到：

```toml
# ── 向量化 ── 云端只用于"把问题变成向量"，每次提问 1 次调用
PKDB_EMBED_BASE_URL = "https://api.siliconflow.cn/v1"
PKDB_EMBED_MODEL = "BAAI/bge-m3"
PKDB_EMBED_DIM = "1024"
PKDB_EMBED_API_KEY = "sk-你的Key"

# ── 对话生成（组织语言）──
PKDB_LLM_BASE_URL = "https://api.siliconflow.cn/v1"
PKDB_LLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"
PKDB_LLM_API_KEY = "sk-你的Key"

# ── 重排（想关掉就设成 "0"）──
PKDB_RERANK_API_KEY = "sk-你的Key"
PKDB_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
```

**两个注意点**：
- TOML 里**值必须加引号**（连数字 `"1024"` 也建议加，本项目按字符串读）
- 三处 Key 是同一把硅基流动 Key

> 上面这份与本地 `.env` 的**当前配置一致**：对话用硅基流动的免费 `Qwen/Qwen2.5-7B-Instruct`。
> （如果哪天把对话换成 DeepSeek，这里还要多一行 `PKDB_LLM_EXTRA_BODY`，见 README「换厂商」。）

⚠️ **绝不要把 `secrets.toml` 提交到仓库** —— 只通过 Streamlit 的界面粘贴。
仓库的 `.gitignore` 里已经忽略了 `.streamlit/secrets.toml`。

保存后应用会自动重启。

---

## 第 5 步：验收清单

打开你的应用地址，逐条确认：

- [ ] 顶部徽标显示 `向量 就绪`、`模型 Qwen/Qwen2.5-7B-Instruct`
- [ ] **没有**"向量未就绪 / 未配置 Key"的横幅
- [ ] 右上角显示 **只读模式 · 索引随仓库提供**（而不是"重建索引"按钮）
- [ ] 随手问一句（如"他在泰雷兹做过什么？"）能出答案 + `[1][2]` 角标 + 可展开的来源
- [ ] 问一句 **「2017在那个公司」应能答出「泰雷兹（Thales）」** ——
      这是**年份索引**的验收点（库里那 84 条 `chunk_years` 记录随 sqlite 一起上云，
      少了它这个问题会答不出来）

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'pkdb'` | Main file path 填错了；`app.py` 顶部已有 sys.path 补丁，正常填 `pkdb/app.py` 不会报 |
| 提示"未配置 Key" | Secrets 变量名拼错，或值没加引号 → 改完点 **Reboot** |
| 语义检索不生效、只剩关键词 | `PKDB_EMBED_API_KEY` 没配 → 查询向量算不出来 |
| 点了"重建索引"却报错 | 不应出现（云端已隐藏该按钮）；若出现说明 `PKDB_SRC_DIR` 指向了不存在的目录 |
| 改了 Secrets 没生效 | 应用菜单里点 **Reboot** |
| 隔一阵打开很慢 | 免费层会休眠，首次访问属冷启动，正常 |
| 想更新简历内容 | **在本地** `python -m pkdb.cli build`，然后把新的 `pkdb.sqlite3` 提交推送 |

---

## 本地 vs 云端

| | 本地 | 云端 |
|---|---|---|
| 数据源 docx | 有（`F:\` 盘） | **无**（也不需要） |
| 建库 / 重建索引 | 可点 | **隐藏，只读** |
| 索引库来源 | 本地生成 | 随仓库提供 |
| 每次提问的 API 调用 | 2–3 次 | 2–3 次（完全一样） |
| 文件系统 | 持久 | **容器重启即重置** |

> **云端不要试图改库**：容器重启会重置文件系统，改了也留不住。
> 更新内容一律「本地 build → 提交 sqlite → 推送」。

---

## 已知坑（都实际踩过，别再踩）

| 现象 | 原因 | 处理 |
|---|---|---|
| 推送被拒：`GH007 ... email privacy restrictions` | GitHub 账号开着「阻止命令行推送暴露我的邮箱」，而提交用的是该账号的**私有邮箱**；**author 与 committer 两个邮箱都会被校验** | 改用 noreply：`235839094+KenzoYamazakiPufm@users.noreply.github.com`。<br>一劳永逸且**只影响本仓库**：`git config user.email "235839094+KenzoYamazakiPufm@users.noreply.github.com"` |
| 创建应用时**下拉框里看不到自己的仓库** | 私有仓库没被授权给 Streamlit | GitHub → Settings → Applications → Authorized OAuth Apps → Streamlit → 把该仓库加进授权范围 |
| 提交信息里的中文变乱码 | PowerShell 向 git 传中文参数会被转码 | 把信息写进 UTF-8 文件，再 `git commit -F msg.txt` |
| 命令"看起来成功了"其实压根没执行 | PowerShell 把 `<...>` 当**重定向符**（例如 `--pretty=format:'...<%ae>...'`），导致**整条命令解析失败** | 别在 git format 串里用 `<` `>`；**执行完必须用独立命令核验结果**，别信它的"成功"输出 |
| `gh` 命令找不到 | PATH 未必刷新到当前 shell 会话 | 用全路径：`& 'C:\Program Files\GitHub CLI\gh.exe' ...` |
