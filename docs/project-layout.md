# 项目结构与服务器目录

[文档中心](README.md)

## 源码目录

```text
README.md                  项目入口与文档导航
docs/                      安装、配置、使用、API、维护和排错
paperflow/
  cli.py                   命令行入口
  workflows.py             检索、入库与下载工作流
  database.py              论文和期刊指标 SQLite
  paths.py                 数据目录解析
  web/                     Web 页面、JSON API、任务队列和 Worker
  sources/                 各论文检索来源适配器
  pdf/                     PDF 获取与校验
  auth.py                  浏览器授权和会话管理
  config.py                CLI 配置加载
deploy/server/             安装器、systemd 单元、配置和 Caddy 示例
scripts/build_server_release.py  源码发行包构建工具
tests/                     自动化测试
.github/workflows/         测试和服务器安装验证
```

`pyproject.toml` 定义 Python 包、入口和依赖。运行 `pip install` 后，服务使用安装环境里的程序，源码修改不会自动上线。

## 服务器安装目录

| 路径 | 用途 |
|---|---|
| `/opt/paperflow-web/.venv` | 当前运行环境链接 |
| `/opt/paperflow-web/venvs/` | 各次安装保留的环境 |
| `/opt/paperflow-web/.portable-install` | 安装器管理标记，不应手工伪造 |
| `/etc/paperflow-web.env` | 配置和私密凭据 |
| `/etc/systemd/system/paperflow-*.service` | Web、Worker 服务单元 |
| `/run/paperflow/worker.lock` | Worker 运行锁 |

## 数据目录

便携安装以 `/var/lib/paperflow` 为数据根目录，包含论文和任务数据库，以及 `downloads/`、`exports/`、`imports/`、`job-logs/`、`sessions/` 等运行目录。应整体备份，不能只备份源码。

本地 CLI 未设置 `PAPERFLOW_DATA_ROOT` 时默认从当前工作目录解析路径；服务器已由 systemd 设置该变量。执行管理命令时显式指定服务器数据目录和数据库，避免意外操作另一份库。具体见 [数据库说明](database.md)。

构建发行包采用明确的文件白名单，包含源码、部署文件、测试和文档，不包含用户数据、实例环境文件或本机运行记录。
