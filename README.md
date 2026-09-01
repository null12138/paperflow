# paperflow — 物种文献检索与 PDF 批量下载

按物种拉丁名（或 DOI 清单）从多数据源检索论文元数据，并尽可能下载 PDF 全文。

检索和下载结果默认增量写入 SQLite 数据库 `paperflow.db`。原有 TXT 报告仍会生成，便于直接查看和兼容旧流程。

## 数据源架构

| 用途 | 来源 | 状态 |
|---|---|---|
| 元数据检索 | **WOS Starter API**（官方 API，分页/断点/配额） | ✅ |
| | **PubMed / Europe PMC** | ✅ |
| | **Crossref** | ✅ |
| | **Semantic Scholar (S2)** | ✅ |
| | **CNKI** | ✅ 内置 Playwright；首次使用需 `paperflow auth login cnki` |
| PDF 下载 | **Sci-Hub**（altcha 验证码自动求解 + DDoS-Guard 会话） | ✅ |
| | **Unpaywall / PMC / Europe PMC**（开放获取） | ✅ |
| | **CNKI PDF**（复用已保存的机构会话，点击站内授权下载） | ✅ |
| | **出版社订阅适配器**（Elsevier/Springer/Wiley/Oxford/Nature/T&F/AAAS…） | 🔧 需先 `auth login` 对应站点 |

> CNKI 与付费墙出版社在无公开 API 的前提下，通过**浏览器授权登录**（学校账号/机构 SSO）后以站点会话访问，不依赖 Kimi WebBridge 等外部扩展。CNKI 会分页抓取列表，并以每秒最多 1 篇的速度补摘要详情。

## 安装（全新系统）

```bash
# macOS / Linux
bash install.sh          # 自动建 .venv、装依赖、装 playwright chromium、生成 .env、自检
source .venv/bin/activate
paperflow doctor         # 随时自检：依赖/浏览器/代理/授权

# Windows
install.bat
```

也可手动：

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
playwright install chromium
```

> 依赖 Playwright 浏览器（授权弹窗与 Sci-Hub 自动会话需要）。

可选环境变量（`.env.example` 有清单）：

```bash
export UNPAYWALL_EMAIL='你的真实邮箱'   # Unpaywall/NCBI 要求
export NCBI_EMAIL='你的真实邮箱'
export WOS_API_KEY='...'               # WOS 官方 Starter API；只写入本机 .env
export S2_API_KEY='...'                # 可提高 Semantic Scholar 配额
export ELSEVIER_API_KEY='...'          # Elsevier Article Retrieval API（需相应访问权限）
export ELSEVIER_INSTTOKEN='...'        # 可选：机构令牌
export SPRINGER_NATURE_API_KEY='...'   # Springer Nature OpenAccess API（仅 OA 内容）
```

S2 未配置 Key 时使用匿名配额，触发 HTTP 429 属于接口限流，不会影响其他数据源；在 `.env` 配置个人 `S2_API_KEY` 后重新检索即可。界面会显示简短的中文提示，不会输出完整请求 URL。

Semantic Scholar 请求在适配器内部统一限制为每秒最多 1 次，即使多个关键词并行检索也不会突破该频率。

ScienceDirect/Elsevier 下载：启用 `publisher` 通道后，DOI 为 `10.1016/...` 的文章会优先调用 Elsevier Article Retrieval API，并校验返回的 PDF 文件头；API 返回权限错误或非 PDF 时自动回退到出版社页面解析。API Key 不绕过机构订阅或反机器人验证。

Springer Nature 下载：配置 `SPRINGER_NATURE_API_KEY` 后，Springer/ BioMed Central 等 DOI 会先查询 Springer Nature OpenAccess API，找到 OA PDF 才下载；付费文章仍回退 SpringerLink 机构授权。

## 使用

### TUI 全屏界面

安装后直接运行：

```bash
paperflow tui
# 或指定另一个数据库
paperflow tui --db data/my-papers.db
```

TUI 包含六个页面：

- **概览**：文章、PDF、待下载、候选和影响因子匹配统计；
- **文献库**：按关键词、来源、下载状态和影响因子范围查询；
- **检索**：只获取元数据并写入 SQLite，不触发下载；
- **WOS批量**：官方 Starter API 分页获取，实时显示进度/当日配额并支持断点续传；
- **下载队列**：稍后从数据库恢复候选并批量下载；
- **影响因子**：导入正式 JIF CSV/TSV 并立即刷新匹配结果。

联网检索、CNKI 浏览器和下载都在后台 worker 中执行，界面不会冻结。按 `r` 刷新，按 `q` 退出；长任务执行时请等待完成后再退出，已有数据库记录不会被删除。

推荐的 TUI 两步流程：进入“检索”页，在 `input.txt` 路径旁点击“导入 TXT”（每行一个关键词），勾选需要的数据源后点击“开始检索”；检索结束后进入“下载队列”，按关键词或元数据来源筛选，选择 PDF 通道并点击“开始下载”。检索和下载彼此独立，任何一步中断都可以稍后从 SQLite 继续。

如需清空数据，在“概览”页点击“清空数据库”，二次确认后执行。程序会先复制出 `*.before-clear-YYYYMMDD-HHMMSS.db.bak` 备份，再清除业务数据并保留数据库表结构。

### 1. 授权（一次性，分发版核心）

```bash
python -m paperflow.cli auth login cnki          # 弹出浏览器 → 手动登录（校园账号）→ 回车
python -m paperflow.cli auth login sciencedirect # 出版社订阅
python -m paperflow.cli auth login scihub        # 自动模式（DDoS-Guard 挑战自动等待）
python -m paperflow.cli auth status              # 查看各站点授权状态
```

登录态保存在 `sessions/<站点>.json`，仅本机使用，请勿提交到版本库。

CNKI 首次配置及验证：

```bash
paperflow auth login cnki
# 在弹出的浏览器完成机构登录，保持页面打开，回终端按回车保存
paperflow search --species "银杏" --sources CNKI --limit 5
# 检索并下载当前机构有权限的 CNKI PDF
printf '银杏\n' > input.txt
paperflow run --input input.txt --sources CNKI --out cnki_downloads --mode cnki --limit 1
```

默认使用可见浏览器，便于处理正常登录或验证码。已有稳定会话后可设置 `CNKI_HEADLESS=1`；如只要列表、不补详情摘要，可设置 `CNKI_FETCH_ABSTRACTS=0`。

### 2. 检索元数据

```bash
# 指定物种 + 数据源
python -m paperflow.cli search --input input.txt --sources PubMed,Crossref,S2 --limit 20
# 结果写到 paperflow.db，同时保留 papers_meta.txt
```

### 3. 按 DOI 批量下载 PDF

```bash
# doi_list.tsv：每行 "DOI<TAB>题名"（纯 DOI 也可）
python -m paperflow.cli download --doi-file doi_list.tsv --out downloads \
    --mode scihub+oa+publisher --rpm 30 --email your@email.com
```

| 参数 | 说明 |
|---|---|
| `--mode` | 下载通道，可组合：`cnki` / `scihub` / `oa` / `publisher`（逗号或 `+` 分隔） |
| `--rpm` | 限速（篇/分钟），默认 30；**稳定优先，1 篇/分钟也行**（`--rpm 1`） |
| `--failed` | 失败清单（DOI、题名、原因） |
| `--db` | SQLite 路径，默认 `paperflow.db` |
| `--keyword` | `download` 导入 DOI 清单时手动关联关键词，可重复指定 |

### 4. 完全解耦：先检索，稍后从数据库下载

检索阶段会把论文、关键词、来源和 PDF 候选全部保存到 SQLite。进程退出、重启电脑后仍可独立下载；CNKI 保存文章详情页，下载时再用当前授权会话生成临时下载地址。

```bash
# 第一步：只检索和入库，不下载
paperflow search --species "银杏" --sources CNKI --limit 20 --db paperflow.db

# 第二步：以后任意时间从数据库下载未尝试项
paperflow download-db --db paperflow.db --keyword "银杏" --source CNKI \
  --mode cnki --status pending --out cnki_downloads

# 重试以前失败的项目
paperflow download-db --db paperflow.db --keyword "银杏" \
  --mode cnki --status failed --out cnki_downloads
```

`download-db` 支持 `--keyword`、`--source`、`--status pending|failed|all`、`--min-if`、`--max-if` 和 `--limit`。Europe PMC、S2 等直接全文候选同样会持久化，因此无 DOI 的开放全文也能稍后下载。

### 5. 影响因子（JIF）

JIF 是 Clarivate 的年度授权数据，项目不会用 CiteScore、SJR 或自行计算值冒充影响因子。请从你有权使用的数据来源导出 CSV/TSV 后导入；导入一次后，历史论文和以后新检索的论文都会按期刊规范名自动匹配最新年份。

文件至少包含以下列（列名也兼容 `Journal name`、`JIF`、`JCR Year` 和中文列名）：

```csv
journal,impact_factor,year
Journal of Example Research,4.5,2024
```

```bash
paperflow impact-factor import --file jcr.csv --source "JCR 2024" --db paperflow.db

# 查看匹配结果或按影响因子筛选
paperflow db list --min-if 5 --max-if 10 --limit 100

# 只下载影响因子不低于 5 的未尝试论文
paperflow download-db --status pending --min-if 5 --out downloads

# 先只预解析 DOI 的 OA/出版社候选，不下载文件
paperflow download-preflight --db paperflow.db --limit 100
```

结果始终同时保存影响因子数值、年份和数据来源；未精确匹配的期刊显示 `-`，不会猜测。

### 6. 全流程（检索 → 下载）

```bash
python -m paperflow.cli run --input input.txt --out downloads --mode cnki+scihub+oa --limit 20
```

CNKI 通道不要求 DOI；它使用检索时保留的文章详情页候选，复用 `sessions/cnki.json`，点击可见的 `PDF Download`。只有文件头为 `%PDF-` 才会记为成功。无机构权限、登录失效或出现验证码时会停止该篇下载并给出提示，不会绕过站点限制。

### 7. SQLite 数据库

每个检索、下载、全流程和报告命令都支持 `--db`。数据库保存文章元数据与摘要，并用关联表记录“一篇文章对应多个关键词、多个检索来源”；下载成功源、PDF 路径和每次下载历史也会保留。

```bash
# 查看统计
python -m paperflow db stats

# 按关键词查看文章
python -m paperflow db list --keyword "Panthera tigris" --limit 20

# 把旧 abstracts.txt / summary.txt 迁移进数据库（可重复执行）
python -m paperflow db import-legacy

# 自动去重：先预览，再执行
python -m paperflow db dedupe --dry-run
python -m paperflow db dedupe

# 使用另一个数据库文件
python -m paperflow search --input input.txt --db data/my-papers.db
```

表结构和字段说明见 [`docs/database.md`](docs/database.md)。

### 8. WOS 官方 API 批量获取

```bash
# 单个关键词，最多 1000 条
paperflow wos-fetch --keyword "Ginkgo biloba" --max-records 1000

# 多个关键词（input.txt 每行一个）；0 表示全部
paperflow wos-fetch --input input.txt --max-records 0 --db paperflow.db
```

每页最多 50 条，默认两次 API 请求间隔 1 秒。数据每页先增量写入 SQLite，然后原子更新
`wos_api_runs/manifest.json`；重新运行同一命令会从已完成条数继续。SQLite 会按 DOI/题名自动去重。

Starter API 提供题名、作者、DOI、期刊和年份，但不保证提供摘要。如需补充 Full Record 字段，
仍可使用你已获授权的 WOS 浏览器导出；`export-wos` 仅作 legacy 备用，新检索链路不依赖它。

## 代码结构

```
paperflow/
  cli.py              统一命令行（search/wos-fetch/download/auth）
  tui.py              Textual 全屏终端界面
  workflows.py        CLI/TUI 共享检索与下载工作流
  auth.py             浏览器授权（弹窗登录 → 捕获 cookie → sessions/）
  net.py              网络层（会话、代理 failover、限速器）
  models.py           论文数据模型与去重
  database.py         SQLite schema、候选队列、JIF 匹配、查询与迁移
  sources/            元数据源适配器（注册表模式，可插拔增删）
    wos.py  pubmed_crossref_s2.py  cnki.py
  pdf/                PDF 引擎：依次尝试 CNKI → Sci-Hub → OA → 出版社
    cnki.py           CNKI 机构会话 + 浏览器下载事件 + PDF 校验
    scihub.py         altcha 求解 + DDoS-Guard 会话
    oa.py             Unpaywall / PMC
    publisher.py      出版社订阅适配（带浏览器授权登录态）
  legacy/             旧版独立脚本（wos_species_downloader 等，保留可参考）
tests/                主包的离线单元测试
```

源码、本机会话、日志和下载结果的完整边界见 [`docs/project-layout.md`](docs/project-layout.md)。现有下载数据不会在安装或测试时被移动、覆盖。

## 开发验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q paperflow
```

如需 pytest：`pip install -e '.[dev]'`。

## 边界与合规提示

- Sci-Hub、付费墙绕过仅应在您有权使用的范围内使用；请遵守学校图书馆与出版社条款。
- `auth login` 只保存您主动登录产生的会话，不读取浏览器其它数据。
- 新文献（2025-2026）Sci-Hub 覆盖率有限（数据库基本停留在 2021 前后）；付费墙文献最可靠的来源是学校订阅（`auth login sciencedirect` 等）。
