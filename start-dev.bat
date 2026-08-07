@echo off
chcp 65001 >nul
title Satrap Dev Launcher

:: 调用 PowerShell 脚本（支持单实例检测和自动清理）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1" %*
