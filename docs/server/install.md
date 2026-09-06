# 服务器安装：从零到可以使用

[文档中心](../README.md) · 下一篇：[配置说明](configuration.md)

目标：在一台新服务器上部署 Paperflow，通过 `https://你的域名` 登录，成功完成一个小型检索任务。本文对应服务器发行版 **0.9.0**，推荐 Ubuntu 24.04。

> 已经运行 Paperflow？先看 [升级与迁移](operations.md)。本教程的首次安装命令不会覆盖已有的手动部署。

## 1. 准备好这些东西

| 项目 | 要求或建议 |
|---|---|
| 服务器系统 | Linux，使用 systemd；以下命令以 Ubuntu 24.04 为例 |
| 登录权限 | root，或有 sudo 权限的用户 |
| Python | 安装器要求 Python 3.10+；Ubuntu 24.04 默认 Python 3.12 |
| 资源 | 建议至少 2 GB 内存、10 GB 可用磁盘；批量 PDF 需要更多空间 |
| 网络 | 服务器能下载 Python 依赖并访问论文数据源 |
| 域名 | 一个可以修改 DNS 的域名或子域名 |
| 入站端口 | SSH 端口及 TCP 80、443；不需要对外开放 8765 |

PDF 与 ZIP 会同时占用磁盘，打包还需要临时空间。SQLite 必须放在本地磁盘，不要放到 rclone、WebDAV 或 FUSE 挂载目录。

没有域名时，可以先完成第 2～5 步，在服务器本机检查服务。完整浏览器流程请完成 HTTPS 配置：应用启用了安全 Cookie，直接用 HTTP 访问可能登录后仍无法提交表单。

## 2. 登录服务器并安装系统依赖

**在本地电脑终端执行**，把 `SERVER_IP` 替换为服务器公网 IP，按实际情况替换 `root`：

```bash
ssh root@SERVER_IP
```

输入的是服务器 SSH 密码，输入过程中不显示字符是正常现象。这个密码与之后的 Paperflow 网页密码无关。

**从这里开始，除非另有说明，所有命令都在服务器 SSH 终端执行。** root 用户也可以省略命令前的 `sudo`。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates git
python3 --version
systemctl --version
free -h
df -h /
```

检查 Python 至少为 3.10、systemctl 可用、根分区空间充足。普通 Docker 容器通常没有运行 systemd，不能直接套用本安装器。

## 3. 下载并解压发行包

下面固定安装 0.9.0，避免安装到尚未发布的开发代码：

```bash
mkdir -p ~/paperflow-install
cd ~/paperflow-install
curl -fL --retry 3 -o paperflow-web-0.9.0.tar.gz \
  https://github.com/null12138/paperflow/releases/download/v0.9.0/paperflow-web-0.9.0.tar.gz
curl -fL --retry 3 -o SHA256SUMS.txt \
  https://github.com/null12138/paperflow/releases/download/v0.9.0/SHA256SUMS.txt
sha256sum --ignore-missing -c SHA256SUMS.txt
tar -xzf paperflow-web-0.9.0.tar.gz
cd paperflow-web-0.9.0
ls deploy/server/install.sh
```

校验应显示 `paperflow-web-0.9.0.tar.gz: OK`，最后应找到安装脚本。若校验失败，停止安装并重新下载。校验文件也包含 ZIP 的哈希，`--ignore-missing` 会跳过未下载的 ZIP。

如果服务器无法访问 GitHub，可以在本地下载发行包和校验文件，再从**本地电脑**上传：

```bash
scp paperflow-web-0.9.0.tar.gz SHA256SUMS.txt root@SERVER_IP:~/paperflow-install/
```

然后回到服务器，从校验、解压继续。上传源码不能免去安装时下载 Python 依赖的网络需求。

**替代方式：Git 克隆。** 与上面的发行包方式二选一即可：

```bash
git clone --branch v0.9.0 --depth 1 https://github.com/null12138/paperflow.git ~/paperflow-source
cd ~/paperflow-source
```

历史 v0.9.0 发行包内保留发布时的 README；重构后的完整指南在仓库主分支 `docs/`。后续从主分支构建的源码包也会包含文档目录。

## 4. 执行首次安装

在含有 `deploy/server/install.sh` 的源码目录执行：

```bash
sudo bash deploy/server/install.sh
```

脚本会创建专用用户、安装 Python 依赖、生成随机登录密码和会话密钥，并启用两个开机自启服务：

| 服务 | 用途 |
|---|---|
| `paperflow-web` | 提供网页、API 和文件下载 |
| `paperflow-worker` | 从队列领取检索与下载任务 |

安装最后应显示 `Installed. Credentials are in /etc/paperflow-web.env`。脚本没有完成时，不要继续配域名来掩盖安装错误，先看 [安装排错](troubleshooting.md)。

安装结果存放在：

| 路径 | 内容 |
|---|---|
| `/opt/paperflow-web/.venv` | 指向当前程序运行环境的链接 |
| `/opt/paperflow-web/venvs/` | 每次安装或升级创建的独立环境 |
| `/etc/paperflow-web.env` | 登录账号、密码、会话密钥和数据源配置 |
| `/var/lib/paperflow/` | 数据库、论文、任务日志、输入和导出文件 |

下载解压的源码目录只是安装输入；编辑里面的 Python 文件不会自动更新正在运行的服务。

## 5. 检查服务，读取登录信息

```bash
sudo systemctl is-active paperflow-web paperflow-worker
curl -fsS http://127.0.0.1:8765/healthz
```

预期分别看到两个 `active`，以及 `{"status":"ok"}`。健康检查只证明 Web 能响应，不代表外部论文数据源全部可用。

读取网页账号与密码（不要把输出发到公开日志）：

```bash
sudo grep -E '^PAPERFLOW_WEB_(USERNAME|PASSWORD)=' /etc/paperflow-web.env
```

默认用户名为 `paperflow`，密码每次首次安装随机生成。需要修改密码或填写来源信息时：

```bash
sudoedit /etc/paperflow-web.env
sudo systemctl restart paperflow-web paperflow-worker
```

首次建议填写自己的 `NCBI_EMAIL` 和 `UNPAYWALL_EMAIL`；有 WOS Starter API 凭据再填写 `WOS_API_KEY`。没有 WOS 权限不会凭空得到 WOS 结果。详细字段见 [配置说明](configuration.md)。

## 6. 配置域名和 HTTPS

### 6.1 在域名控制台设置 DNS

例如你准备使用 `paperflow.example.com`：添加子域名 `paperflow` 的 A 记录，值为服务器公网 IPv4。只有服务器 IPv6 确实可达时才配置 AAAA 记录。

在云服务器安全组中放行 TCP 80 和 443，保留 SSH 端口。若服务器还启用了 UFW，也要放行相同端口；不要为了本教程盲目启用或重置防火墙。8765 保持仅本机监听。

DNS 生效后，在服务器执行下列命令，应解析到预期地址：

```bash
getent ahosts paperflow.example.com
```

本文使用 Caddy 管理 HTTPS。证书签发要求域名指向服务器且验证端口可达，详细机制见 Caddy 官方文档：

```text
https://caddyserver.com/docs/automatic-https
```

### 6.2 安装 Caddy

```bash
sudo apt-get install -y caddy
caddy version
```

若系统仓库找不到该包，按 Caddy 官方安装指南添加适合系统的软件源，再继续：

```text
https://caddyserver.com/docs/install
```

如果服务器已有 Nginx、Apache 或其他入口占用 80/443，应在现有入口配置 HTTPS 和反向代理到 `127.0.0.1:8765`。不要直接停止已有网站；下面步骤用于采用 Caddy 的服务器。

### 6.3 新增 Paperflow 站点

```bash
sudo mkdir -p /etc/caddy/conf.d
sudoedit /etc/caddy/conf.d/paperflow.caddy
```

写入以下配置，**将第一行域名替换成自己的域名**，保存退出：

```caddyfile
paperflow.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8765
}
```

然后编辑主配置：

```bash
sudoedit /etc/caddy/Caddyfile
```

保留已有站点，在所有站点花括号之外添加下面一行；已存在时不要重复添加：

```caddyfile
import /etc/caddy/conf.d/*.caddy
```

检查并应用：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
curl -fsS https://paperflow.example.com/healthz
```

校验成功后才 reload。最后应返回 `{"status":"ok"}`。如有证书或 502 错误，看 [HTTPS 排错](troubleshooting.md)。

## 7. 登录网页并完成第一个任务

**在本地电脑浏览器**打开 `https://你的域名`，使用第 5 步的 Paperflow 账号和密码登录。

1. 在首页选择“只获取摘要与 DOI”。
2. 输入一个关键词，例如 `Ginkgo biloba`，将检索上限设为 `10`。
3. 提交后进入“任务进度”，确认任务从排队进入运行。
4. 等任务结束，查看日志中各来源的成功、错误和结果数量，下载 TXT。
5. 打开“文献库”，确认能看到入库的题目等信息。
6. 再提交一个小型“检索并下载 PDF”任务，下载 ZIP，按 [结果结构](../usage.md) 核对。

任务结束不代表所有论文都能下载。先验证元数据检索，再验证 PDF，能更容易区分数据源问题和全文访问问题。首次不要直接提交不限量的大任务。

**完成标准：** HTTPS 页面能登录、Web 和 Worker 都运行、小检索能结束并输出 TXT、文献库有记录。之后按 [配置说明](configuration.md) 调整参数，并按 [维护指南](operations.md) 做备份。
