@echo off
chcp 65001 >nul
title Satrap UI Dev Server

:: 项目根目录 (脚本在 scripts 子目录中)
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

:: 清理残留进程, 隐藏启动控制服务与聊天服务, 并完成健康检查
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start-background-services.ps1" -ProjectRoot "%PROJECT_ROOT%"
if errorlevel 1 (
    echo Failed to start Satrap background services
    exit /b 1
)

cd /d "%PROJECT_ROOT%\satrap-ui"

echo Starting Satrap UI development server...
echo.

:: 检查 node_modules 是否存在
if not exist "node_modules" (
    echo Installing dependencies...
    call npm install
    echo.
)

:: 启动开发服务器
echo Starting Vite dev server at http://localhost:5173
echo Press Ctrl+C to stop
echo.
call npm run dev
