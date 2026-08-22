# Satrap 开发环境启动脚本
# 功能: 启动控制服务和前端, 保证单实例运行, 前端关闭时自动停止后端

param(
    [switch]$Force   # 强制重启且不询问
)

$ErrorActionPreference = "SilentlyContinue"

# 项目根目录 (脚本位于 scripts 子目录)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DataDir = Join-Path $ProjectRoot ".satrap"

# 确保数据目录存在
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

# ============================================================
# 检查端口是否被占用
# ============================================================
function Test-PortInUse {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $connection
}

# ============================================================
# 停止全部 Satrap 服务
# ============================================================
function Stop-SatrapServices {
    Write-Host "Stopping existing Satrap services..." -ForegroundColor Yellow
    
    # 通过控制服务 API 停止
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -TimeoutSec 2 | Out-Null
    } catch {}
    
    Start-Sleep -Milliseconds 500
    
    # 强制终止 Satrap 相关 Python 进程
    $processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | 
        Where-Object { $_.CommandLine -match "satrap" }
    
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped process PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
    
    # 清理 PID 文件
    Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue
    
    Write-Host "All services stopped" -ForegroundColor Green
}

# ============================================================
# 检查现有实例
# ============================================================
$hasExisting = $false
$ports = @(19871, 19870, 19872, 5173)
$portNames = @("Control Server", "Backend", "Chat Server", "Frontend")

for ($i = 0; $i -lt $ports.Count; $i++) {
    if (Test-PortInUse -Port $ports[$i]) {
        Write-Host "[Detected] $($portNames[$i]) port $($ports[$i]) is in use" -ForegroundColor Yellow
        $hasExisting = $true
    }
}

if ($hasExisting) {
    Write-Host ""
    Write-Host "Existing Satrap services detected!" -ForegroundColor Red
    
    if (-not $Force) {
        $choice = Read-Host "Stop old instances and restart? (Y/N)"
        if ($choice -ne "Y" -and $choice -ne "y") {
            Write-Host "Startup cancelled" -ForegroundColor Yellow
            exit 1
        }
    }
    
    Stop-SatrapServices
    Start-Sleep -Seconds 2
}

# ============================================================
# 启动控制服务 (完全隐藏)
# ============================================================
Write-Host "Starting control server..." -ForegroundColor Cyan

# 使用 Start-Process 直接启动隐藏窗口
$pythonPath = (Get-Command python).Source
$controlArgs = "-m satrap.core.backend.control_server"

Start-Process -FilePath $pythonPath -ArgumentList $controlArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden

# 等待控制服务启动
Start-Sleep -Seconds 2

# 验证控制服务是否成功启动
$controlRunning = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:19871/status" -Method Get -TimeoutSec 1
        $controlRunning = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if ($controlRunning) {
    Write-Host "Control server started (http://127.0.0.1:19871)" -ForegroundColor Green
} else {
    Write-Host "Warning: Control server may not have started properly" -ForegroundColor Yellow
}

# ============================================================
# 启动聊天服务 (完全隐藏)
# ============================================================
Write-Host "Starting chat server..." -ForegroundColor Cyan

$chatArgs = "-m satrap.display.server"

Start-Process -FilePath $pythonPath -ArgumentList $chatArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden

# 等待聊天服务启动
Start-Sleep -Seconds 1

# 验证聊天服务是否成功启动
$chatRunning = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:19872/api/chat/health" -Method Get -TimeoutSec 1
        $chatRunning = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if ($chatRunning) {
    Write-Host "Chat server started (http://127.0.0.1:19872)" -ForegroundColor Green
} else {
    Write-Host "Warning: Chat server may not have started properly" -ForegroundColor Yellow
}

# ============================================================
# 启动前端 (监控模式, 关闭时自动停止后端)
# ============================================================
Write-Host "Starting frontend dev server..." -ForegroundColor Cyan

$frontendDir = Join-Path $ProjectRoot "satrap-ui"

$frontendScript = @"
`$Host.UI.RawUI.WindowTitle = 'Satrap Frontend'
Set-Location '$frontendDir'

# 注册退出事件
`$cleanup = {
    Write-Host "`nStopping backend service..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:19871/stop' -Method Post -TimeoutSec 2 | Out-Null
        Write-Host "Backend stopped" -ForegroundColor Green
    } catch {}
}

# 捕获 Ctrl+C 和窗口关闭事件
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action `$cleanup | Out-Null

try {
    npm run dev
} finally {
    & `$cleanup
}
"@

$frontendScriptPath = Join-Path $env:TEMP "satrap_frontend.ps1"
$frontendScript | Out-File -FilePath $frontendScriptPath -Encoding UTF8

# 启动前端窗口
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$frontendScriptPath`"" -WindowStyle Normal

# 清理临时脚本 (延迟执行以确保前端已读取)
Start-Sleep -Seconds 1
Remove-Item $frontendScriptPath -Force -ErrorAction SilentlyContinue

# ============================================================
# 显示启动信息
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Satrap Dev Environment Started" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Control Server:  http://127.0.0.1:19871 (hidden)" -ForegroundColor White
Write-Host "  Chat Server:     http://127.0.0.1:19872 (hidden)" -ForegroundColor White
Write-Host "  Frontend UI:     http://localhost:5173" -ForegroundColor White
Write-Host ""
Write-Host "  Use Dashboard 'Start Backend' button to start backend" -ForegroundColor Gray
Write-Host ""
Write-Host "  Closing frontend window will auto-stop backend" -ForegroundColor Gray
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 自动关闭启动器窗口
Write-Host "This window will close in 3 seconds..." -ForegroundColor Gray
Start-Sleep -Seconds 3
