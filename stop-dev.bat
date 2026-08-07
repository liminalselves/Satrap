@echo off
chcp 65001 >nul
title Satrap Stop Services

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-dev.ps1"

pause
