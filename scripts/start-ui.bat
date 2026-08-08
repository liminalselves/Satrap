@echo off
chcp 65001 >nul
title Satrap UI Dev Server

:: 项目根目录（脚本在 scripts 子目录中）
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

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
