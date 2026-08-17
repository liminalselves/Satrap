# Kill stale Satrap chat server processes (satrap.display.server)
# Called by start-ui.bat before starting a new chat server

$processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'satrap\.display' }

if ($processes) {
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Killed old chat server PID: $($proc.ProcessId)" -ForegroundColor Yellow
        } catch {}
    }
    # Wait for port release
    Start-Sleep -Milliseconds 500
} else {
    Write-Host "  No stale chat server found" -ForegroundColor Gray
}
