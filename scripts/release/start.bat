@chcp 65001 >nul
@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0runtime\python.exe" -X utf8 "%~dp0release\launcher.py" start %*
set SATRAP_EXIT_CODE=%ERRORLEVEL%
if not "%SATRAP_EXIT_CODE%"=="0" pause
exit /b %SATRAP_EXIT_CODE%
