# AI / JSON API 指南

[文档中心](README.md) · [部署服务](server/install.md)

先完成 HTTPS 部署。以下命令可在能访问服务的电脑或 AI 运行环境执行。将示例域名和用户名换成自己的值，不要把服务器 SSH 密码当作网页密码。


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


## 一次完整的调用流程

1. 提交任务并记录返回的任务 ID，不要反复 POST 同一任务。
2. 用任务 ID 查询状态，建议间隔 3～5 秒；网络请求超时不代表后台任务取消。
3. 成功后读取响应中的 `txt_url` 或 `archive_url`。链接若为相对路径，在前面加上本站 HTTPS 地址。
4. 下载文件时继续使用同一组认证信息。下面以纯检索任务 1 为例，实际 ID 以响应为准：

```bash
curl -fL -u paperflow \
  'https://paperflow.example.com/jobs/1/results.txt' -o task-1-results.txt
```

HTTP `401` 表示认证缺失或错误；`400` 通常表示输入或参数不正确。文件未就绪时先查询任务状态，不要把错误响应保存成 PDF 或 ZIP。`succeeded` 后仍应阅读任务日志和下载汇总以确认部分来源失败的情况。
