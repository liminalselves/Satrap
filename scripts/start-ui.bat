@echo off
chcp 65001 >nul
title Satrap UI Dev Server

:: 项目根目录（脚本在 scripts 子目录中）
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

:: 杀掉残留的旧聊天服务进程 (防止旧代码占用 19872 端口)
echo Checking for existing chat server...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%kill-chat-server.ps1"
timeout /t 1 /nobreak >nul

:: 启动聊天服务 (隐藏窗口, 需在项目根目录下)
echo Starting chat server...
pushd "%PROJECT_ROOT%"
start "Satrap Chat Server" /min python -m satrap.display.server
popd
timeout /t 2 /nobreak >nul

cd /d "%PROJECT_ROOT%\satrap-ui"

echo Starting Satrap UI development server...
echo.

:: Check if node_modules exists
if not exist "node_modules" (
    echo Installing dependencies...
    call npm install
    echo.
)

:: Start dev server
echo Starting Vite dev server at http://localhost:5173
echo Press Ctrl+C to stop
echo.
call npm run dev
