@chcp 65001 >nul
@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
pushd "%~dp0"
"%~dp0runtime\python.exe" -X utf8 -m satrap %*
set SATRAP_EXIT_CODE=%ERRORLEVEL%
popd
exit /b %SATRAP_EXIT_CODE%
