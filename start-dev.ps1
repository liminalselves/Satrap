# Satrap 开发环境启动脚本
# 功能：启动控制服务和前端，确保单实例运行，前端关闭时自动停止后端

param(
    [switch]$Force  # 强制重启，不询问
)

$ErrorActionPreference = "SilentlyContinue"

# 项目根目录
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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
# 停止所有 Satrap 服务
# ============================================================
function Stop-SatrapServices {
    Write-Host "正在停止现有 Satrap 服务..." -ForegroundColor Yellow
    
    # 通过控制服务 API 停止
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -TimeoutSec 2 | Out-Null
    } catch {}
    
    Start-Sleep -Milliseconds 500
    
    # 强制终止 Satrap 相关 Python 进程
    $processes = Get-WmiObject Win32_Process -Filter "Name='python.exe'" | 
        Where-Object { $_.CommandLine -match "satrap" }
    
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  已停止进程 PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
    
    # 清理 PID 文件
    Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue
    
    Write-Host "所有服务已停止" -ForegroundColor Green
}

# ============================================================
# 检查是否有已有实例
# ============================================================
$hasExisting = $false
$ports = @(19871, 19870, 5173)
$portNames = @("控制服务", "后端服务", "前端服务")

for ($i = 0; $i -lt $ports.Count; $i++) {
    if (Test-PortInUse -Port $ports[$i]) {
        Write-Host "[检测] $($portNames[$i]) 端口 $($ports[$i]) 已被占用" -ForegroundColor Yellow
        $hasExisting = $true
    }
}

if ($hasExisting) {
    Write-Host ""
    Write-Host "检测到已有 Satrap 服务在运行！" -ForegroundColor Red
    
    if (-not $Force) {
        $choice = Read-Host "是否停止旧实例并重新启动？(Y/N)"
        if ($choice -ne "Y" -and $choice -ne "y") {
            Write-Host "已取消启动" -ForegroundColor Yellow
            exit 1
        }
    }
    
    Stop-SatrapServices
    Start-Sleep -Seconds 2
}

# ============================================================
# 启动控制服务（完全隐藏）
# ============================================================
Write-Host "正在启动控制服务..." -ForegroundColor Cyan

$controlServerScript = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d `"$ProjectRoot`" && python -m satrap.core.backend.control_server", 0, False
"@

$vbsPath = Join-Path $env:TEMP "start_satrap_control.vbs"
$controlServerScript | Out-File -FilePath $vbsPath -Encoding ASCII
cscript //nologo $vbsPath
Remove-Item $vbsPath -Force -ErrorAction SilentlyContinue

# 等待控制服务启动
Start-Sleep -Seconds 2

# 验证控制服务是否启动成功
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
    Write-Host "控制服务已启动 (http://127.0.0.1:19871)" -ForegroundColor Green
} else {
    Write-Host "警告: 控制服务可能未正常启动" -ForegroundColor Yellow
}

# ============================================================
# 启动前端（监控模式，关闭时自动停止后端）
# ============================================================
Write-Host "正在启动前端开发服务器..." -ForegroundColor Cyan

$frontendScript = @"
`$Host.UI.RawUI.WindowTitle = 'Satrap Frontend'
Set-Location '$ProjectRoot\satrap-ui'

# 注册退出事件
`$cleanup = {
    Write-Host "`n正在停止后端服务..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:19871/stop' -Method Post -TimeoutSec 2 | Out-Null
        Write-Host "后端已停止" -ForegroundColor Green
    } catch {}
}

# 捕获 Ctrl+C 和窗口关闭
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

# 清理临时脚本（延迟删除，确保前端已读取）
Start-Sleep -Seconds 1
Remove-Item $frontendScriptPath -Force -ErrorAction SilentlyContinue

# ============================================================
# 显示启动信息
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Satrap 开发环境已启动" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  控制服务:  http://127.0.0.1:19871 (隐藏)" -ForegroundColor White
Write-Host "  前端界面:  http://localhost:5173" -ForegroundColor White
Write-Host ""
Write-Host "  使用 Dashboard 的 '启动后端' 按钮启动后端服务" -ForegroundColor Gray
Write-Host ""
Write-Host "  关闭前端窗口将自动停止后端服务" -ForegroundColor Gray
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 自动关闭启动窗口
Write-Host "此窗口将在 3 秒后关闭..." -ForegroundColor Gray
Start-Sleep -Seconds 3
