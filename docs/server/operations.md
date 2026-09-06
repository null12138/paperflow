# 服务器维护：状态、升级、备份和迁移

[文档中心](../README.md) · [配置说明](configuration.md) · [故障排查](troubleshooting.md)

以下命令在服务器执行，适用于 `deploy/server/install.sh` 创建的便携安装。

## 查看运行状态与版本

```bash
sudo systemctl status paperflow-web paperflow-worker --no-pager
curl -fsS http://127.0.0.1:8765/healthz
sudo journalctl -u paperflow-web -u paperflow-worker -n 100 --no-pager
readlink -f /opt/paperflow-web/.venv
/opt/paperflow-web/.venv/bin/python -c 'from importlib.metadata import version; print(version("paperflow"))'
```

版本命令查询服务使用的安装环境。源码目录的 Git 版本和已安装版本可以不同，`git pull` 不会自动更新服务。

持续看日志使用 `sudo journalctl -u paperflow-worker -f`，按 Ctrl+C 只退出日志查看，不会停止任务。`healthz` 能响应并不证明 Worker 或外部数据源都正常，要结合两个服务和任务日志判断。

## 重启

```bash
sudo systemctl restart paperflow-web paperflow-worker
sudo systemctl is-active paperflow-web paperflow-worker
```

建议在任务空闲时操作。中断任务的恢复可能重新执行部分检索或下载，不保证从每一个来源的精确分页位置继续。

## 升级便携安装

1. 等正在运行的任务结束，按下一节备份数据和配置。
2. 从 Releases 下载目标版本和对应校验文件，校验后解压至新的源码目录，不覆盖旧源码目录。
3. `cd` 到新版本源码根目录，执行：

```bash
sudo bash deploy/server/install.sh --upgrade
```

4. 执行本文开头的状态与版本检查，再从 HTTPS 页面做一个小任务。

安装器先安装新环境，再切换 `.venv` 链接并重启服务；保留原配置、数据和旧环境。升级后的健康检查失败时会尝试切回旧 Python 环境。这个机制不是数据库、服务单元和配置的完整快照，不能代替备份。

若提示 `already exists without the portable-install marker`，说明目录可能来自手动部署。不要手工创建标记强行覆盖，按本文迁移步骤处理。

## 备份

备份包含论文、数据库及登录/API 密钥，应放在受控存储中。以下例子先停止两个服务，保证 SQLite 和文件处于稳定状态；完成后无论打包是否成功都会尝试重新启动服务。

先确认 `/mnt/backup` **已经挂载独立备份磁盘且容量足够**，然后运行：

```bash
mountpoint /mnt/backup
df -h /mnt/backup
```

只有确认上述检查符合预期才继续；按实际挂载路径修改脚本：

```bash
sudo bash <<'BACKUP'
set -euo pipefail
mountpoint -q /mnt/backup
backup_file="/mnt/backup/paperflow-$(date +%Y%m%d-%H%M%S).tar.gz"
umask 077
trap 'systemctl start paperflow-web paperflow-worker' EXIT
systemctl stop paperflow-worker paperflow-web
tar -czf "$backup_file" -C / etc/paperflow-web.env var/lib/paperflow
tar -tzf "$backup_file" >/dev/null
printf 'Backup saved: %s\n' "$backup_file"
BACKUP
```

再检查两个服务是 `active`。不要在已经接近满盘的根分区额外压缩全部 PDF。没有独立磁盘时先准备远端备份方案。不要在服务运行时删除 SQLite 的 WAL/SHM 文件。

## 恢复到新服务器

这是恢复**便携安装备份**的流程；旧手动部署见下一节。

1. 在新服务器安装与备份兼容的版本，检查能正常启动。
2. 把备份放到新服务器，先用 `tar -tzf 备份文件` 检查内部路径应为 `etc/paperflow-web.env` 和 `var/lib/paperflow/...`。
3. 停止新服务器上的两个服务。恢复会覆盖同路径文件，只在空的新实例操作；已有新数据要先另外备份。
4. 替换下面的备份路径后执行：

```bash
sudo systemctl stop paperflow-worker paperflow-web
sudo tar -xzf /mnt/backup/paperflow-YYYYMMDD-HHMMSS.tar.gz -C /
sudo chown -R paperflow:paperflow /var/lib/paperflow
sudo chown root:paperflow /etc/paperflow-web.env
sudo chmod 0640 /etc/paperflow-web.env
sudo systemctl start paperflow-web paperflow-worker
```

5. 配置新服务器域名/HTTPS，检查文献库、历史任务和实际 PDF 下载，再切换 DNS。

恢复会带回旧账号、密码、密钥和任务状态，可能恢复未完成任务，因此备份最好在任务空闲时进行。上述数据备份不包含 Caddy 站点配置和系统服务文件；服务由安装器重建，HTTPS 按安装教程重新配置。

## 从旧手动部署迁移

带有 `/opt/paperflow-web/current` 等自定义目录的部署不一定使用便携安装布局。先在新服务器安装测试，不要直接对原目录运行 `--upgrade`。

迁移前记录旧服务的 `ExecStart`、环境文件位置、数据根目录和运行版本；停止旧服务，备份完整数据与配置。数据库可能保存 PDF 的绝对路径，需要保持相同路径，或先完成路径迁移再验证文件下载。只复制 `.db` 文件无法迁移论文文件。

旧配置不要整份盲目覆盖新配置：逐项转移登录、来源凭据和下载参数，保留新安装的用户、数据目录和锁配置。确认新站点能读旧库、下载旧 PDF、运行新任务后，再切换入口和退役旧服务。

## 查看磁盘占用与旧环境

```bash
df -h /
sudo du -xhd1 /var/lib/paperflow /opt/paperflow-web /var/log
readlink -f /opt/paperflow-web/.venv
sudo du -sh /opt/paperflow-web/venvs/*
```

`df` 查看分区剩余空间，`du` 查看目录占用。通常 PDF、导出 ZIP 和保留的旧环境值得先检查。只删除确认不再需要且已有备份的结果文件；不要删除正在使用的数据库或 `.venv` 指向的环境。

安装器保留历史虚拟环境供排查和回退，不会自动清理。确认新版本稳定后，再逐个清理不再需要的旧环境，保留至少一个可用回退版本。删除数据目录里的 PDF 会影响文献库中的下载链接。
