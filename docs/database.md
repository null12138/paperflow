# SQLite 数据库

默认文件是根目录的 `paperflow.db`，可用所有核心命令的 `--db` 参数修改。数据库由 Python 标准库 `sqlite3` 管理，启用外键和 WAL；重复检索会合并记录，不会反复插入同一篇文章。

## 表结构

| 表 | 内容 |
|---|---|
| `papers` | 题名、DOI/PMID/PMCID、摘要、年份、期刊及规范名、作者、PDF 路径、最终下载源、下载详情、失败原因 |
| `keywords` | 检索关键词；当前物种检索时就是物种拉丁名 |
| `paper_keywords` | 文章与关键词的多对多关系 |
| `sources` | 元数据检索来源，如 WOS、PubMed、Crossref、CNKI |
| `paper_sources` | 文章与检索来源的多对多关系 |
| `pdf_candidates` | 检索时发现的直接 PDF 或详情页候选；用于跨进程独立下载 |
| `journal_metrics` | 正式导入的影响因子数值、年份、来源和期刊规范名 |
| `download_attempts` | 每次下载结果、实际成功源、详情、PDF 路径和时间 |

`papers.download_source` 的常见值：

- `oa`：Unpaywall、PMC 或 Europe PMC 通道；
- `publisher`：出版社或学校订阅通道；
- `scihub`：Sci-Hub 通道；
- `local`：复用目标目录中已有的有效 PDF；
- `legacy`：从旧 `summary.txt` 迁移，原报告无法确定更精确的下载源。

## 常用查询

```bash
python -m paperflow db stats
python -m paperflow db list --limit 50
python -m paperflow db list --keyword "Ginkgo biloba"
python -m paperflow db list --min-if 5 --max-if 10
python -m paperflow db dedupe --dry-run
python -m paperflow db dedupe
```

## 检索与下载解耦

```bash
paperflow search --species "银杏" --sources CNKI --db paperflow.db
paperflow download-db --keyword "银杏" --source CNKI --mode cnki --status pending
paperflow download-db --keyword "银杏" --source CNKI --mode cnki --status failed
```

`pending` 只包含从未尝试过且尚无 PDF 的论文，`failed` 只包含有失败历史且尚无 PDF 的论文，`all` 是两者合集。成功下载会自动退出后续未下载队列。

## 影响因子

导入合法取得的年度 JIF CSV/TSV：

```bash
paperflow impact-factor import --file jcr.csv --source "JCR 2024"
```

程序按规范化期刊全名精确匹配，并在多个年度中默认展示最新年份。每个结果都保留数值、年度和来源；未匹配时留空，不做模糊猜测，也不以其他引用指标替代 JIF。

也可直接使用系统 `sqlite3`：

```sql
SELECT title, abstract, pdf_path, download_source
FROM papers
WHERE pdf_path <> '';
```

## 旧数据迁移

```bash
python -m paperflow db import-legacy \
  --abstracts abstracts.txt \
  --summary summary.txt \
  --db paperflow.db
```

迁移是幂等的：文章、关键词、来源关系不会重复；同一路径的 legacy 下载记录也只写入一次。

## 自动去重

新数据写入时会自动按 DOI、PMID 和规范化题名去重。对已有数据库可运行 `db dedupe`：它会选择元数据更完整的记录作为主记录，并合并关键词、检索来源和下载历史。

为避免把“同题名但实际不同”的文章误合并，具有冲突 DOI、PMID 或 PMCID 的组会自动跳过。建议先加 `--dry-run` 查看数量，再执行正式合并。
