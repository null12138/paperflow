# Paperflow

自托管的论文检索、文献库和 PDF 下载工具。提供 Web 页面、持久化后台任务、JSON API 与命令行。

**服务器发行版：0.9.0。** 适合在自己的 Linux VPS 上运行；使用本地 SQLite 保存论文和任务，关闭网页后任务继续执行。

## 功能

- 网页提交关键词或 DOI，检索题名、作者、期刊、年份、摘要等信息。
- 默认关键词来源：**WOS + PubMed + Europe PMC**。Europe PMC 查询其可索引的全文，包括正文、文后内容；WOS 和 PubMed 补充元数据检索。不等于搜索所有出版商的全部付费正文。
- Crossref、Semantic Scholar（S2）适配器可通过命令行显式指定。分页代码支持继续取回结果；`limit=0` 不设应用层总篇数上限，但仍受来源覆盖、接口配额和实际响应限制。
- 文献入库去重、关键词分类、下载结果追踪。文献库可查题名、DOI、期刊、摘要、关键词和来源。
- 只检索任务输出 TXT；下载任务生成按任务、关键词分类的 ZIP。
- AI 接口使用 HTTP Basic Auth；可创建任务、查询状态、检索文献库、下载单篇 PDF 或 ZIP。
- 影响因子从导入的 CSV/TSV 存入 SQLite，按期刊关联到论文。**本版本不提供自动抓取 JCR 或逐篇在线查询影响因子。**

## 服务器部署指南

### 1. 准备服务器

部署脚本支持 **Ubuntu / Debian + systemd + Python 3.10 或更新版本**；推荐 Ubuntu 24.04 / Python 3.12。

建议预留至少 2 GB 内存和 10 GB 可用磁盘。实际需要取决于论文大小：PDF 和 ZIP 会同时占空间，不能仅按论文篇数估算。下载默认在可用空间低于 3 GB 时等待；**打包仍需要额外临时空间**。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates git
```

源码安装需要联网获取 Python 依赖。发布包包含源码和部署脚本，不包含 Python、操作系统包、浏览器二进制或完整离线依赖。

### 2. 获取发行版并安装

从 [Releases](https://github.com/null12138/paperflow/releases) 下载 `paperflow-web-0.9.0.tar.gz`，上传到服务器后：

```bash
tar -xzf paperflow-web-0.9.0.tar.gz
cd paperflow-web-0.9.0
sudo bash deploy/server/install.sh
```

也可以克隆源码：

```bash
git clone --branch v0.9.0 --depth 1 https://github.com/null12138/paperflow.git
cd paperflow
sudo bash deploy/server/install.sh
```

脚本会创建 `paperflow` 系统用户、独立虚拟环境和两个 systemd 服务，自动生成随机登录密码及会话密钥。

| 路径 | 内容 |
|---|---|
| `/opt/paperflow-web/.venv` | 指向当前 Python 环境的链接 |
| `/opt/paperflow-web/venvs/` | 各次安装的 Python 环境 |
| `/etc/paperflow-web.env` | 登录凭据、API key、数据与下载配置 |
| `/var/lib/paperflow/` | SQLite、PDF、输入、日志和导出文件 |
| `/run/paperflow/worker.lock` | Worker 单实例锁 |

**不要把 SQLite 数据根目录放在 rclone/WebDAV/FUSE 挂载盘上。** 本部署使用本地磁盘；网盘可用于另外备份完成的文件。

### 3. 设置登录和来源配置

```bash
sudo cat /etc/paperflow-web.env
sudoedit /etc/paperflow-web.env
sudo systemctl restart paperflow-web paperflow-worker
```

默认用户名为 `paperflow`，密码由安装器随机生成，可自行修改。不要直接照抄别人的线上密码。

| 配置项 | 用途 |
|---|---|
| `PAPERFLOW_WEB_USERNAME` / `PAPERFLOW_WEB_PASSWORD` | 网页和 API 共用的登录凭据 |
| `PAPERFLOW_WEB_SECRET` | 会话签名密钥；安装时自动生成 |
| `WOS_API_KEY` | WOS Starter API；未配置时不能保证 WOS 数据源可用 |
| `UNPAYWALL_EMAIL` | Unpaywall 使用的联系邮箱，建议填写 |
| `NCBI_EMAIL` / `NCBI_API_KEY` | PubMed 联系邮箱、可选 API key |
| `S2_API_KEY` | 可选 S2 API key；匿名请求可能限流 |
| `OPENALEX_API_KEY` | 可选开放文献解析凭据 |
| `PAPERFLOW_DOWNLOAD_WORKERS` | 默认 4 个下载线程 |
| `PAPERFLOW_DOWNLOAD_BATCH_SIZE` | 默认每批 50 篇 |
| `PAPERFLOW_MIN_FREE_GB` | 默认保留 3 GB 本地可用空间 |
| `PAPERFLOW_PROXIES` | 可选代理；留空使用默认网络配置 |

本安装布局固定采用 `/var/lib/paperflow` 和 `127.0.0.1:8765`。若要改变数据路径、监听端口或服务用户，需要同步修改 systemd 配置和健康检查，不要只改一个环境变量。

浏览器驱动不是默认 Web 下载流程的必要条件，因此服务器安装不会下载 Chromium。如使用 CLI 浏览器授权通道，需要另外安装 Playwright 浏览器及对应系统依赖；配置好可访问的浏览器会话后再使用。

### 4. 配置域名和 HTTPS

先将你的域名 A/AAAA 记录指向服务器，并确保 80/443 端口可达。推荐使用 Caddy 反向代理。

```bash
sudo apt-get install -y caddy
sudo mkdir -p /etc/caddy/conf.d
sudo cp deploy/server/Caddyfile.example /etc/caddy/conf.d/paperflow.caddy
sudoedit /etc/caddy/conf.d/paperflow.caddy
```

把示例域名换成自己的域名：

```caddyfile
paperflow.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8765
}
```

在 `/etc/caddy/Caddyfile` 中添加下面一行（已有该配置则不要重复添加；保留其他站点配置）：

```caddyfile
import /etc/caddy/conf.d/*.caddy
```

检查并加载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

之后打开 `https://你的域名/`，输入配置文件中的账号密码。应用使用 Secure Cookie，**正式网页操作需要 HTTPS**；直接访问 HTTP 不适合作为最终部署。若使用 Cloudflare，配置与源站 HTTPS 一致的 Full (strict) 模式。

### 5. 确认服务正常

```bash
systemctl status paperflow-web paperflow-worker --no-pager
curl -fsS http://127.0.0.1:8765/healthz
sudo journalctl -u paperflow-web -u paperflow-worker -n 80 --no-pager
```

健康检查应返回 `{"status":"ok"}`。访问其他接口不带凭据时返回 `401` 是预期行为。

第一次先提交一个小任务（例如设置上限 10 篇），确认来源配置、磁盘空间和下载能力，再提交不限量任务。来源失败可能导致部分结果；任务成功不代表所有来源成功或所有 PDF 可用，应结合任务日志和 ZIP 的汇总核对。

## 使用方式与输出结构

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

## AI / JSON API

`/ai` 提供使用说明，`/api/v1` 提供机器可读接口摘要（不是 OpenAPI 规范文件）。下例将域名替换成你的域名；`curl -u paperflow` 会提示输入密码。

创建检索任务：

```bash
curl -u paperflow 'https://paperflow.example.com/api/v1/jobs' \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"metadata","items":["Ginkgo biloba"],"limit":10}'
```

创建关键词检索和下载任务：

```bash
curl -u paperflow 'https://paperflow.example.com/api/v1/jobs' \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"download","mode":"keyword","items":["Ginkgo biloba"],"limit":10}'
```

DOI 下载：

```json
{"workflow":"download","mode":"doi","items":["10.1038/s41586-021-03819-2"],"limit":10}
```

成功入队返回 HTTP `202`。每隔几秒查询任务，直到 `succeeded`、`failed` 或 `cancelled`：

```bash
curl -u paperflow 'https://paperflow.example.com/api/v1/jobs/1'
curl -u paperflow 'https://paperflow.example.com/api/v1/papers?q=Ginkgo&limit=20'
```

| 接口 | 功能 |
|---|---|
| `GET /api/v1/papers?q=...&limit=20&offset=0` | 查询共享文献库；不是发起新的外部检索 |
| `GET /api/v1/papers/<id>` | 论文元数据、PDF 下载链接 |
| `GET /api/v1/papers/<id>/pdf` | 下载单篇 PDF |
| `POST /api/v1/jobs` | 新建检索或下载任务 |
| `GET /api/v1/jobs/<id>` | 状态、日志、`archive_url`、`txt_url` |
| `GET /api/v1/jobs/<id>/archive` | 成功下载任务的 ZIP |
| `GET /jobs/<id>/results.txt` | 成功纯检索任务的 TXT |
| `GET /api/v1/impact-factors?q=Nature&limit=50` | 查询已导入期刊指标 |

所有文件下载也需要 Basic Auth。机器客户端用 `application/json` 和认证头，不带浏览器 `Origin` / `Sec-Fetch-Site` 头时无需 CSRF token；浏览器操作仍要求 CSRF。数据源和下载模式由现有服务流程决定，不支持向该任务接口任意传入 CLI 命令。

## 导入影响因子

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

## 升级、备份与迁移

### 升级便携安装

下载并解压新发行包，在新目录中执行：

```bash
sudo bash deploy/server/install.sh --upgrade
```

升级先创建新虚拟环境，依赖安装成功后再切换服务；保留配置、数据和旧环境。健康检查失败时尝试切回旧环境。重启可能中断正在运行的任务并触发恢复，建议任务完成后升级。

旧环境放在 `/opt/paperflow-web/venvs/`；确认新版本正常后，可查看 `.venv` 的链接目标，再手动删除不再使用的旧环境，避免累积占用。

### 备份

停止服务后再打包 SQLite 和数据目录，备份会包含密钥，请妥善保管：

```bash
sudo systemctl stop paperflow-worker paperflow-web
sudo tar -czf /path/on/backup-disk/paperflow-backup.tar.gz \
  /etc/paperflow-web.env /var/lib/paperflow
sudo systemctl start paperflow-web paperflow-worker
```

建议保存到另一块磁盘或将备份上传至远端，避免在满盘的 VPS 上额外生成大备份。不要在运行中删除 SQLite 的 WAL/SHM 文件，也不要删除尚未同步的 rclone 写缓存。

### 已有手动部署

安装器不会直接覆盖没有 `.portable-install` 标记的 `/opt/paperflow-web`。如果已有自定义部署，先在新服务器验证发行版；迁移时停止旧服务、备份配置和完整数据目录、安装新服务，再恢复数据并调整目录属主为 `paperflow:paperflow`。数据库中已有 PDF 绝对路径时，应保持原数据路径或另做路径迁移，不能只复制数据库。

## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests -q
```

打包：

```bash
python3 scripts/build_server_release.py
```

输出 `dist/paperflow-web-0.9.0.tar.gz`、`.zip` 和 SHA-256 校验文件。构建使用明确的源码白名单，不收集本机 `.env`、cookies、论文 PDF、SQLite 或实例部署记录。

## 许可证

代码采用 [MIT License](LICENSE)。第三方 Python 包与论文内容适用各自许可证；请仅获取和使用你有权访问的文献资源。
