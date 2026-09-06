# 故障排查

[文档中心](../README.md) · [安装教程](install.md) · [维护指南](operations.md)

先定位问题发生在哪一层：安装器 → Web / Worker → HTTPS → 数据源 → PDF / 打包。下面命令都在服务器执行。

## 先收集这三项

```bash
sudo systemctl status paperflow-web paperflow-worker --no-pager
curl -fsS http://127.0.0.1:8765/healthz
sudo journalctl -u paperflow-web -u paperflow-worker -n 100 --no-pager
```

分享日志前去掉密码、API key、cookies 和个人数据，不要直接贴出整个环境文件。

## 安装阶段

| 现象 | 检查与处理 |
|---|---|
| `Run as root` | 使用 `sudo bash deploy/server/install.sh` |
| `Linux with systemd is required` | 在有 systemd 的 Linux VPS 上安装；macOS 和普通容器不能直接使用该脚本 |
| `Python 3.10+ required` | 检查 `python3 --version`，换用符合要求的环境；本教程推荐 Ubuntu 24.04 |
| venv / ensurepip 缺失 | 安装与 Python 对应的 venv 包；教程使用 `python3-venv` |
| pip 下载超时或证书错误 | 检查 DNS、系统时间、代理与依赖站点连通性；修复网络后重试，不要禁用证书验证 |
| `Already installed. Use --upgrade` | 已有便携安装，在目标版本源码目录使用 `--upgrade` |
| `without the portable-install marker` | 可能是手动部署，也可能是首次安装依赖阶段失败留下目录；先检查目录和服务，不能直接补标记或删除目录。手动部署按维护指南迁移；首次失败先修复根因并确认无业务数据后再处理残留目录 |
| `Health check failed` | 看两个服务日志、端口和配置；升级可能已切回旧环境，重新查询实际版本 |

## 网页打不开或 HTTPS 报错

**本机 healthz 也失败：** 先修复 Web 服务，检查日志和 8765 端口，不要先调整 DNS。

```bash
sudo ss -ltnp | grep -E ':(80|443|8765)\b'
```

**本机正常，域名返回 502：** 检查反向代理是否指向 `127.0.0.1:8765`，以及 Caddy 是否运行在同一台机器。

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo journalctl -u caddy -n 100 --no-pager
```

**证书签发失败或连接超时：** 检查域名 A/AAAA 是否正确、云安全组与系统防火墙是否放行 80/443、其他程序是否占用端口。如果没有可达 IPv6，删除错误的 AAAA 记录。使用代理 DNS 服务时还要核对其到源站的 HTTPS 设置。

**401：** `/healthz` 不要求登录，其他业务入口需要 Paperflow 的账号密码。检查 `/etc/paperflow-web.env` 中的用户名和密码，修改后重启两个服务。浏览器缓存旧凭据时可用无痕窗口验证。

**能打开页面，但表单 CSRF 错误：** 确认地址是 HTTPS，重新加载页面登录后再提交。应用安全 Cookie 不适合直接通过公网 HTTP 使用。

## 任务一直排队或等待

Web 和 Worker 是两个服务。网页能打开不代表 Worker 在执行任务：

```bash
sudo systemctl is-active paperflow-worker
sudo journalctl -u paperflow-worker -n 150 --no-pager
df -h /
```

先检查前面是否还有运行任务，再检查日志是否提示空间不足、来源超时或锁占用。默认可用空间低于 3 GB 会等待；释放空间后会继续检查。不要在已有 Worker 运行时另开手动 Worker，也不要直接删除活动锁文件。

## 检索数量比预期少

依次核对：

1. 本次任务上限是否设置为 10 等小值；`0` 才表示无应用层总上限。
2. 查询的是文献库，还是新提交的外部检索任务。
3. 关键词、年份、检索字段和来源是否与比较对象一致；全文包含不是主题检索。
4. WOS API 权限是否有效，日志中是否有 401、403、429、超时或分页错误。
5. 统计的是原始命中、去重后论文，还是已经下载的 PDF。

Web 默认使用 WOS、PubMed、Europe PMC。Crossref/S2 不是当前网页默认来源。任务整体成功仍可能伴随部分来源失败，应逐个看来源日志；把上限调大无法修复权限或网络问题。

## PDF 少、ZIP 没有 PDF

检索到元数据不保证能够获取全文，来源权限、开放获取状态和出版社响应都会影响下载。先查看任务日志和 ZIP 内每个关键词的 `summary.txt`：`downloaded:` 是成功取得有效 PDF 的论文，`failedDownload:` 是未取得 PDF 的论文。

纯检索任务只提供 TXT，不生成 ZIP。下载任务可能包含摘要和汇总但没有成功 PDF，此时保留空的 `pdf_downloaded/` 是预期行为。文件尚未打包完成时先查看任务状态；空间不足时检查 PDF、ZIP 和临时打包空间。

预期目录见 [网页使用和结果文件](../usage.md)。不要只凭任务状态 `succeeded` 判断所有 PDF 都下载成功。

## 影响因子为空

影响因子来自事先导入的期刊指标表，并非自动联网逐篇查询。确认已导入 CSV/TSV、期刊名称匹配、年份与来源字段正确。操作见 [影响因子导入](../usage.md)。
