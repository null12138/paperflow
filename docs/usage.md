# 网页使用和结果文件

[文档中心](README.md) · [安装教程](server/install.md)


首页两种关键词流程：

1. **检索并下载 PDF**：关键词检索、元数据入库、下载全文并生成 ZIP。
2. **只获取摘要与 DOI**：检索并输出 TXT，不生成 PDF ZIP。

下载任务解压结构：

```text
task-1/
├── 01_关键词一/
│   ├── abstracts.txt
│   ├── summary.txt
│   └── pdf_downloaded/
├── 02_关键词二/
│   ├── abstracts.txt
│   ├── summary.txt
│   └── pdf_downloaded/
└── manifest.json
```

- `abstracts.txt` 包含题目、作者、期刊、年份、DOI、摘要；摘要缺失会明确标注。
- `summary.txt` 按参考文献格式列出论文，成功下载列在 `downloaded:`，没有有效 PDF 的列在 `failedDownload:`。
- 即使没有 PDF，`pdf_downloaded/` 目录也保留。关键词匹配范围包含文后引用，因此可能出现正文提及该词但不是以该物种为主题的论文。
- 同一论文匹配多个关键词时，可出现在多个分类下；同名 PDF 使用编号避免覆盖。
- 新全流程任务在下载前保存任务语料快照。旧任务兼容从历史报告和关键词关系恢复，`manifest.json` 会注明范围依据。
- DOI 输入没有关键词时，归入 `DOI清单/`。服务器直接生成有该结构的 ZIP，解压后得到任务文件夹，不额外复制一份全部 PDF。


## 如何理解结果数量

首页用于提交任务，“任务进度”用于查看执行状态和日志，“文献库”查询已经入库的论文。查询文献库不会重新向外部数据库搜索。

网页关键词流程默认使用 WOS、PubMed、Europe PMC。Crossref 和 S2 可以在 CLI 中显式指定，API 当前不提供任意来源覆盖参数。全文包含查询只适用于来源实际索引的文本，不等于所有来源都检索全文。

上限填 `0` 表示不设应用层总篇数上限，仍受检索范围、去重、来源权限、配额和返回结果限制。不要把页面总命中数、原始来源结果数、去重后入库数和成功 PDF 数当成同一个数字。

## 影响因子


准备 UTF-8 CSV/TSV，例如下面是**演示数据**：

```csv
journal,impact_factor,year
Example Journal,3.2,2024
```

```bash
sudo install -o paperflow -g paperflow -m 0640 metrics.csv /var/lib/paperflow/imports/metrics.csv
sudo -u paperflow env PAPERFLOW_DATA_ROOT=/var/lib/paperflow \
  /opt/paperflow-web/.venv/bin/paperflow impact-factor import \
  --file /var/lib/paperflow/imports/metrics.csv \
  --db /var/lib/paperflow/paperflow.db --source JCR
```

随后可在 `/impact-factors` 或 API 中查询。来源标签由导入者提供，不代表系统验证了其真实性。指标按期刊名关联并选用库中较新年份，不是每篇论文自身的影响因子。
