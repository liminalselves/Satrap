# Satrap Dev Environment Startup Script
# Features: Start control server and frontend, ensure single instance, auto-stop backend when frontend closes

param(
    [switch]$Force  # Force restart without asking
)

$ErrorActionPreference = "SilentlyContinue"

# Project root directory (script is in scripts subdirectory)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DataDir = Join-Path $ProjectRoot ".satrap"

# Ensure data directory exists
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

# ============================================================
# Check if port is in use
# ============================================================
function Test-PortInUse {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $connection
}

# ============================================================
# Stop all Satrap services
# ============================================================
function Stop-SatrapServices {
    Write-Host "Stopping existing Satrap services..." -ForegroundColor Yellow
    
    # Stop via control server API
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -TimeoutSec 2 | Out-Null
    } catch {}
    
    Start-Sleep -Milliseconds 500
    
    # Force kill Satrap related Python processes
    $processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | 
        Where-Object { $_.CommandLine -match "satrap" }
    
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped process PID: $($proc.ProcessId)" -ForegroundColor Gray
        } catch {}
    }
    
    # Clean up PID files
    Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue
    
    Write-Host "All services stopped" -ForegroundColor Green
}

# ============================================================
# Check for existing instances
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
# Start control server (completely hidden)
# ============================================================
Write-Host "Starting control server..." -ForegroundColor Cyan

# Use Start-Process to launch hidden window directly
$pythonPath = (Get-Command python).Source
$controlArgs = "-m satrap.core.backend.control_server"

Start-Process -FilePath $pythonPath -ArgumentList $controlArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden

# Wait for control server to start
Start-Sleep -Seconds 2

# Verify control server started successfully
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
# Start chat server (completely hidden)
# ============================================================
Write-Host "Starting chat server..." -ForegroundColor Cyan

$chatArgs = "-m satrap.display.server"

Start-Process -FilePath $pythonPath -ArgumentList $chatArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden

# Wait for chat server to start
Start-Sleep -Seconds 1

# Verify chat server started successfully
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
# Start frontend (monitoring mode, auto-stop backend on close)
# ============================================================
Write-Host "Starting frontend dev server..." -ForegroundColor Cyan

$frontendDir = Join-Path $ProjectRoot "satrap-ui"

$frontendScript = @"
`$Host.UI.RawUI.WindowTitle = 'Satrap Frontend'
Set-Location '$frontendDir'

# Register exit event
`$cleanup = {
    Write-Host "`nStopping backend service..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:19871/stop' -Method Post -TimeoutSec 2 | Out-Null
        Write-Host "Backend stopped" -ForegroundColor Green
    } catch {}
}

# Capture Ctrl+C and window close
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action `$cleanup | Out-Null

try {
    npm run dev
} finally {
    & `$cleanup
}
"@

$frontendScriptPath = Join-Path $env:TEMP "satrap_frontend.ps1"
$frontendScript | Out-File -FilePath $frontendScriptPath -Encoding UTF8

# Start frontend window
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$frontendScriptPath`"" -WindowStyle Normal

# Clean up temp script (delayed to ensure frontend has read it)
Start-Sleep -Seconds 1
Remove-Item $frontendScriptPath -Force -ErrorAction SilentlyContinue

# ============================================================
# Display startup info
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

# Auto close launcher window
Write-Host "This window will close in 3 seconds..." -ForegroundColor Gray
Start-Sleep -Seconds 3
