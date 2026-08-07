# Satrap 停止脚本
# 停止所有 Satrap 相关服务

$ErrorActionPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = Join-Path $ProjectRoot ".satrap"

Write-Host "正在停止 Satrap 服务..." -ForegroundColor Yellow

# 通过控制服务 API 停止
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -TimeoutSec 2 | Out-Null
    Write-Host "  控制服务已收到停止指令" -ForegroundColor Gray
} catch {
    Write-Host "  控制服务未运行或无法连接" -ForegroundColor Gray
}

Start-Sleep -Milliseconds 500

# 强制终止 Satrap 相关 Python 进程
$processes = Get-WmiObject Win32_Process -Filter "Name='python.exe'" | 
    Where-Object { $_.CommandLine -match "satrap" }

if ($processes) {
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  已停止进程 PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
} else {
    Write-Host "  没有找到运行中的 Satrap 进程" -ForegroundColor Gray
}

# 停止前端开发服务器（Node.js）
$nodeProcesses = Get-WmiObject Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -match "satrap-ui" -or $_.CommandLine -match "vite" }

if ($nodeProcesses) {
    foreach ($proc in $nodeProcesses) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  已停止前端进程 PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
}

# 清理 PID 文件
Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "所有 Satrap 服务已停止" -ForegroundColor Green
