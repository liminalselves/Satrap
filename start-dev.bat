@echo off
chcp 65001 >nul
title Satrap Full Stack Dev

echo Starting Satrap Control Server, Backend and Frontend...
echo.

:: Start control server in new window
start "Satrap Control" cmd /k "cd /d %~dp0 && python -m satrap.core.backend.control_server"

:: Wait for control server to start
timeout /t 2 /nobreak >nul

:: Start frontend in new window
start "Satrap Frontend" cmd /k "cd /d %~dp0satrap-ui && npm run dev"

echo.
echo Control Server: http://localhost:19871
echo Backend API:    http://localhost:19870 (start via Dashboard)
echo Frontend UI:    http://localhost:5173
echo.
echo Services started in separate windows.
echo Use the Dashboard "启动后端" button to start the backend.
echo.
pause
