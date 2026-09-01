#!/usr/bin/env bash
# paperflow 一键安装（全新系统可用）
# 用法: bash install.sh    （macOS/Linux；Windows 见 README 或 install.bat）

set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
echo "==> 检查 Python ($PY)"
if ! command -v "$PY" >/dev/null; then
  echo "错误: 未找到 python3，请先安装 https://www.python.org/downloads/"
  exit 1
fi

echo "==> 创建虚拟环境 .venv"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> 安装依赖（含 playwright 浏览器，首次约 1-2 分钟）"
python -m pip install --upgrade pip -q
python -m pip install -e . -q
python -m playwright install chromium 2>/dev/null || python -m playwright install chromium

echo "==> 生成环境配置 .env（如不存在）"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  已生成 .env，请补填邮箱:"
  echo "    echo 'export UNPAYWALL_EMAIL=you@example.com' >> .env"
fi

echo "==> 自检 doctor"
python -m paperflow.cli doctor || true

echo
echo "安装完成! 使用方式:"
echo "  source .venv/bin/activate"
echo "  paperflow tui                               # 全屏终端界面"
echo "  paperflow auth status                       # 查看授权"
echo "  paperflow auth login sciencedirect          # 弹浏览器授权学校账号"
echo "  paperflow download --doi-file doi_list.tsv --out downloads --mode publisher+oa+scihub"
echo "详细见 README.md"
