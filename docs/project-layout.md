# 项目结构与本地数据

## 核心源码

```text
paperflow/              可安装的 Python 包
  cli.py                命令行编排
  tui.py                Textual 全屏终端界面
  workflows.py          CLI/TUI 共享的检索、入库和下载队列工作流
  auth.py               浏览器授权和会话管理
  config.py             自动加载本机 `.env`
  models.py             数据模型与去重
  database.py           SQLite 持久化、查询和旧文本迁移
  net.py                网络会话、代理和限速
  sources/              WOS、PubMed、Crossref、S2、CNKI 等检索适配器
  pdf/                  OA、出版社及其他 PDF 获取通道
  legacy/               已归档的早期独立脚本
tests/                  当前主包的离线单元测试
```

根目录的 `pyproject.toml` 是安装与依赖的权威配置；`requirements.txt` 保留给习惯使用 `pip install -r` 的环境。

安装后可运行 `paperflow tui` 打开数据库概览、文献库、检索、下载队列和影响因子页面。长任务由后台 worker 执行，核心数据仍全部进入同一个 SQLite。

## 本地运行数据

以下内容由运行过程生成或只对当前机器有效，已写入 `.gitignore`：

```text
sessions/               浏览器登录态，可能含敏感 cookie
wos_exports/            WOS 导出记录
downloads/              默认下载目录
pdf_downloaded/         report 命令的 PDF 结果
scihub_downloads/       旧下载器结果
unpaywall_downloads/    旧下载器结果（当前目录约 2 GB）
*.log                   运行日志
*.tsv                   DOI、重试和失败中间清单
abstracts.txt           report 输出
summary.txt             report 输出
paperflow.db*            SQLite 主库及 WAL/SHM 临时文件
```

这些目录没有被移动或删除，以保证现有命令、断点续跑和用户数据继续可用。需要归档时，建议按任务整体复制到项目外的日期目录。

远程代理通过 `PAPERFLOW_PROXIES` 环境变量配置（多个地址用逗号分隔），不要把带账号密码的代理 URL 写入源码。

CNKI 使用项目内置 Playwright 和 `sessions/cnki.json`，不需要 Kimi WebBridge。首次运行 `paperflow auth login cnki`，登录完成后回终端按回车保存会话。

## 历史项目

`download_papers/` 是一个带独立 `.git` 的上游旧项目，并且当前存在未提交改动。它与 `paperflow/legacy/` 的用途不同，因此保持原位，不纳入主项目的结构调整。
