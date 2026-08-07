@echo off
chcp 65001 >nul
title Satrap UI Dev Server

cd /d "%~dp0satrap-ui"

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
