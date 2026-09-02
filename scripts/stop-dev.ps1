# Satrap 停止脚本
# 停止全部 Satrap 相关服务

$ErrorActionPreference = "SilentlyContinue"
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# 项目根目录 (脚本位于 scripts 子目录)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DataDir = Join-Path $ProjectRoot ".satrap"
$token = $env:SATRAP_API_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    $tokenPath = Join-Path $DataDir "api-token"
    if (Test-Path -LiteralPath $tokenPath) {
        $token = (Get-Content -Raw -Encoding UTF8 -LiteralPath $tokenPath).Trim()
    }
}
$authHeaders = if ([string]::IsNullOrWhiteSpace($token)) { @{} } else { @{ Authorization = "Bearer $token" } }

Write-Host "Stopping Satrap services..." -ForegroundColor Yellow

# 通过控制服务 API 停止
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -Headers $authHeaders -TimeoutSec 2 | Out-Null
    Write-Host "  Control server received stop command" -ForegroundColor Gray
} catch {
    Write-Host "  Control server not running or unreachable" -ForegroundColor Gray
}

Start-Sleep -Milliseconds 500

# 强制终止 Satrap 相关 Python 进程
$processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | 
    Where-Object { $_.CommandLine -match "satrap" }

if ($processes) {
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped process PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
} else {
    Write-Host "  No running Satrap processes found" -ForegroundColor Gray
}

# 停止前端开发服务 (Node.js)
$nodeProcesses = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -match "satrap-ui" -or $_.CommandLine -match "vite" }

if ($nodeProcesses) {
    foreach ($proc in $nodeProcesses) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped frontend process PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
}

# 清理 PID 文件
Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "All Satrap services stopped" -ForegroundColor Green
