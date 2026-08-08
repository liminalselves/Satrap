# Satrap Stop Script
# Stop all Satrap related services

$ErrorActionPreference = "SilentlyContinue"

# Project root directory (script is in scripts subdirectory)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DataDir = Join-Path $ProjectRoot ".satrap"

Write-Host "Stopping Satrap services..." -ForegroundColor Yellow

# Stop via control server API
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:19871/shutdown" -Method Post -TimeoutSec 2 | Out-Null
    Write-Host "  Control server received stop command" -ForegroundColor Gray
} catch {
    Write-Host "  Control server not running or unreachable" -ForegroundColor Gray
}

Start-Sleep -Milliseconds 500

# Force kill Satrap related Python processes
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

# Stop frontend dev server (Node.js)
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

# Clean up PID files
Remove-Item -Path (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DataDir "backend.lock") -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "All Satrap services stopped" -ForegroundColor Green
