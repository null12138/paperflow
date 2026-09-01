@echo off
REM paperflow 一键安装（Windows）
cd /d "%~dp0"
where python >nul 2>nul || (echo 请先安装 Python 3.9+ && exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
python -m pip install -e . -q
python -m playwright install chromium
if not exist .env copy .env.example .env
echo 安装完成! 运行 paperflow tui 打开全屏终端界面
pause
