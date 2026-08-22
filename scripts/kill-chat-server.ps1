# 终止残留的 Satrap 聊天服务进程 (satrap.display.server)
# 由 start-ui.bat 在启动新聊天服务前调用

$processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'satrap\.display' }

if ($processes) {
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Killed old chat server PID: $($proc.ProcessId)" -ForegroundColor Yellow
        } catch {}
    }
    # 等待端口释放
    Start-Sleep -Milliseconds 500
} else {
    Write-Host "  No stale chat server found" -ForegroundColor Gray
}
