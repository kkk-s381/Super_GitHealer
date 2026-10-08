@echo off
chcp 65001 >nul
title GitHealer Web 控制台
echo =========================================================
echo   🚀 GitHealer 自愈式代码智能体 Web 控制台
echo   👉 正在为您拉起服务并打开浏览器: http://127.0.0.1:7860
echo =========================================================

cd /d "%~dp0"

:: 延迟 1 秒后自动在默认浏览器中打开控制台网页
start "" http://127.0.0.1:7860

:: 优先使用项目内的 .venv 虚拟环境启动，若无则尝试全局 python
if exist "..\.venv\Scripts\python.exe" (
    "..\.venv\Scripts\python.exe" web_app.py --port 7860
) else (
    python web_app.py --port 7860
)

pause
