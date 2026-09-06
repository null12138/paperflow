# Paperflow

在自己的服务器上检索论文、管理文献库、下载 PDF。通过网页或 AI / JSON API 提交任务，关闭网页后后台继续执行。

**第一次部署？从 [服务器安装教程](docs/server/install.md) 开始。** 教程以 Ubuntu 24.04 为例，从 SSH 登录写到 HTTPS 访问和第一个检索任务，每一步都有检查方法。

## 文档导航

| 你想做什么 | 阅读这里 |
|---|---|
| 在新服务器安装 | [服务器安装：从零到可以使用](docs/server/install.md) |
| 修改密码、配置数据源、调整下载参数 | [配置说明](docs/server/configuration.md) |
| 搜索论文、下载结果、理解 ZIP 目录 | [网页使用和结果文件](docs/usage.md) |
| 让 AI 或脚本调用 | [AI / API 指南](docs/api.md) |
| 查看状态、升级、备份、迁移 | [服务器维护](docs/server/operations.md) |
| 打不开网页、任务不动、结果太少 | [故障排查](docs/server/troubleshooting.md) |
| 浏览所有文档 | [文档中心](docs/README.md) |

## 能做什么

- 按关键词或 DOI 获取题名、作者、期刊、年份和摘要，入库去重。
- 默认关键词来源为 WOS、PubMed、Europe PMC；WOS 需要相应接口权限。Europe PMC 可以检索其索引中的正文和文后内容，并不覆盖所有出版商的付费全文。
- 纯检索任务提供 TXT；下载任务提供“任务 → 关键词 → 摘要、汇总、PDF”的 ZIP。
- 网页和 API 共用登录凭据；AI 可以创建任务、查看进度、查询文献库和下载文件。
- 导入期刊影响因子 CSV/TSV 后存入 SQLite，并关联文献库条目；不自动抓取 JCR。

服务器发行版 **0.9.0** 使用 Python、systemd 和本地 SQLite。安装器要求 Linux + systemd + Python 3.10 以上；推荐教程中的 Ubuntu 24.04。发行包是源码包，安装仍需联网获取依赖。下载地址见 [GitHub Releases](https://github.com/null12138/paperflow/releases)。

## 开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests -q
python3 scripts/build_server_release.py
```

构建产物位于 `dist/`，包含部署脚本和完整文档，不收集实例配置、SQLite、论文 PDF 或 cookies。开发目录见 [项目结构](docs/project-layout.md)，数据库 CLI 见 [数据库说明](docs/database.md)。

代码采用 [MIT License](LICENSE)。第三方依赖和论文内容适用各自许可证，请使用你有权访问的文献资源。
