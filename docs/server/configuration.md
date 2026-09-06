# 服务器配置说明

[文档中心](../README.md) · [安装教程](install.md) · [维护指南](operations.md)

## 在哪里改，怎样生效

便携安装的实际配置文件是 `/etc/paperflow-web.env`。它由 systemd 注入 Web 和 Worker；编辑源码目录的 `.env` 不会自动修改这两个服务。

```bash
sudoedit /etc/paperflow-web.env
sudo systemctl restart paperflow-web paperflow-worker
sudo systemctl is-active paperflow-web paperflow-worker
```

文件采用 `KEY=value`，不加 `export`，一行一项。普通值可以不加引号，含空格的值使用引号；它不是 shell 脚本，不会替你执行变量替换或命令。更改前等待任务完成，重启可能中断当前工作。

## 登录与监听

| 字段 | 默认/用途 |
|---|---|
| `PAPERFLOW_WEB_USERNAME` | 默认 `paperflow`，网页和 API 共用 |
| `PAPERFLOW_WEB_PASSWORD` | 安装器随机生成；可改成自己的密码 |
| `PAPERFLOW_WEB_SECRET` | 自动生成的会话签名密钥；保持稳定，修改会使原会话失效 |
| `PAPERFLOW_WEB_HOST` | `127.0.0.1`，经 HTTPS 反向代理访问 |
| `PAPERFLOW_WEB_PORT` | `8765` |
| `PAPERFLOW_DATA_ROOT` | `/var/lib/paperflow`，本地持久化数据 |
| `PAPERFLOW_WORKER_LOCK` | `/run/paperflow/worker.lock`；Worker unit 也设置此项 |

安装器布局固定使用上述数据目录和端口。改变它们需要同步检查服务单元、反向代理、文件权限和安装器健康检查，不能只改环境变量就认为迁移完成。正常部署请保留默认值。

## 论文来源与下载凭据

未取得的 key 留空，不要填写 `YOUR_API_KEY` 之类占位文本。配置 key 不代表自动把该来源加入 Web 检索流程。

| 字段 | 用途 |
|---|---|
| `WOS_API_KEY` | WOS Starter API 凭据；无有效权限可能跳过或失败 |
| `NCBI_EMAIL` | PubMed 请求的联系邮箱，建议填写自己的邮箱 |
| `NCBI_API_KEY` | 可选 NCBI 凭据 |
| `UNPAYWALL_EMAIL` | Unpaywall OA 解析使用的联系邮箱，建议填写 |
| `S2_API_KEY` | Semantic Scholar 凭据；Web 默认来源不含 S2 |
| `OPENALEX_API_KEY` | OpenAlex 解析凭据 |
| `ELSEVIER_API_KEY` / `ELSEVIER_INSTTOKEN` | 对应出版社授权通道 |
| `SPRINGER_NATURE_API_KEY` | 对应出版社接口凭据 |
| `PAPERFLOW_PROXIES` | 可选代理地址；留空不显式指定应用代理，仍可能受运行环境代理设置影响 |

默认关键词检索来源是 WOS、PubMed、Europe PMC。未配置 WOS 的服务器可以先核对其他来源的结果，但不能期待得到 WOS 的完整命中数。

安装器不下载 Chromium。默认 Web 下载流程不需要浏览器；CLI 浏览器授权通道需另外准备 Playwright 浏览器、系统依赖和有权限的会话。

## 下载并发和磁盘

| 字段 | 默认 | 含义 |
|---|---|---|
| `PAPERFLOW_DOWNLOAD_WORKERS` | `4` | PDF 下载线程数 |
| `PAPERFLOW_DOWNLOAD_BATCH_SIZE` | `50` | 每批处理的论文数量 |
| `PAPERFLOW_LOCAL_DISK_PATH` | `/` | 检查可用空间的文件系统路径 |
| `PAPERFLOW_MIN_FREE_GB` | `3` | 下载等待所用的最低可用空间阈值 |
| `PAPERFLOW_STORAGE_WAIT_SECONDS` | `30` | 空间不足后的等待检查间隔（秒） |

低配服务器先保留默认值；出现来源限流时增加并发通常不会改善结果。空间检查不是磁盘配额，也不能保证 ZIP 打包空间足够。不要通过把阈值改成 0 来解决满盘，先检查 [磁盘占用](operations.md)。

完整模板见 [paperflow.env.example](../../deploy/server/paperflow.env.example)。不要把实例配置提交到 Git 或放进发行包。
