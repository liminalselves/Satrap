# Satrap 后台服务生命周期脚本
# 负责清理并隐藏启动控制服务和聊天服务

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$StopOnly
)

$ErrorActionPreference = "Stop"
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$DataDir = Join-Path $ProjectRoot ".satrap"
$Services = @(
    [PSCustomObject]@{
        Name = "控制服务"
        Module = "satrap.core.backend.control_server"
        Port = 19871
        HealthUrl = "http://127.0.0.1:19871/status"
        StopBackend = $true
    },
    [PSCustomObject]@{
        Name = "聊天服务"
        Module = "satrap.display.server"
        Port = 19872
        HealthUrl = "http://127.0.0.1:19872/api/chat/health"
        StopBackend = $false
    }
)

function Test-ModuleCommandLine {
    param(
        [AllowNull()][string]$CommandLine,
        [string]$Module
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    $escapedModule = [Regex]::Escape($Module)
    return $CommandLine -match "(?i)(?:^|\s)-m\s+$escapedModule(?:\s|$)"
}

function Get-ModuleProcesses {
    param([string]$Module)

    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -match "(?i)^(?:python(?:w|\d+(?:\.\d+)?)?|py)\.exe$" -and
                (Test-ModuleCommandLine -CommandLine $_.CommandLine -Module $Module)
            }
    )
}

function Get-PortOwnerIds {
    param([int]$Port)

    return @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Stop-ServiceInstance {
    param([PSCustomObject]$Service)

    $moduleProcesses = @(Get-ModuleProcesses -Module $Service.Module)
    $moduleProcessIds = @($moduleProcesses | Select-Object -ExpandProperty ProcessId)
    $portOwnerIds = @(Get-PortOwnerIds -Port $Service.Port)
    $ownsPort = @($portOwnerIds | Where-Object { $moduleProcessIds -contains $_ }).Count -gt 0

    if ($ownsPort -and $Service.StopBackend) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:19871/stop" -Method Post -TimeoutSec 5 | Out-Null
        } catch {
            Write-Host "  $($Service.Name)未能通过接口停止平台后端, 将继续清理服务进程" -ForegroundColor DarkYellow
        }
    }

    if ($ownsPort) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$($Service.Port)/shutdown" -Method Post -TimeoutSec 2 | Out-Null
        } catch {}
        Start-Sleep -Milliseconds 700
    }

    foreach ($process in @(Get-ModuleProcesses -Module $Service.Module)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Write-Host "  已停止$($Service.Name), PID $($process.ProcessId)" -ForegroundColor DarkGray
    }

    Start-Sleep -Milliseconds 300
    $remainingOwnerIds = @(Get-PortOwnerIds -Port $Service.Port)
    if ($remainingOwnerIds.Count -eq 0) {
        return
    }

    foreach ($ownerId in $remainingOwnerIds) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        $ownerDescription = "PID $ownerId"
        if ($null -ne $owner) {
            $ownerDescription = "$($owner.Name), PID $ownerId"
        }
        throw "$($Service.Name)端口 $($Service.Port) 被其他进程占用: $ownerDescription"
    }
}

function Wait-ServiceHealth {
    param(
        [PSCustomObject]$Service,
        [Diagnostics.Process]$Process
    )

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($Process.HasExited) {
            throw "$($Service.Name)进程提前退出, 退出码 $($Process.ExitCode)"
        }

        try {
            Invoke-RestMethod -Uri $Service.HealthUrl -Method Get -TimeoutSec 5 | Out-Null
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    throw "$($Service.Name)健康检查超时: $($Service.HealthUrl)"
}

function Stop-BackgroundServices {
    foreach ($service in $Services) {
        Stop-ServiceInstance -Service $service
    }

    Remove-Item -LiteralPath (Join-Path $DataDir "control_server.pid") -Force -ErrorAction SilentlyContinue
}

try {
    Write-Host "检查并清理旧的控制服务与聊天服务..." -ForegroundColor Yellow
    Stop-BackgroundServices

    if ($StopOnly) {
        Write-Host "后台服务已停止" -ForegroundColor Green
        return
    }

    $pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
    $startedProcesses = @()

    foreach ($service in $Services) {
        Write-Host "正在隐藏启动$($service.Name)..." -ForegroundColor Cyan
        $process = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @("-m", $service.Module) `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -PassThru
        $startedProcesses += $process
        Wait-ServiceHealth -Service $service -Process $process
        Write-Host "  $($service.Name)已就绪, PID $($process.Id), 端口 $($service.Port)" -ForegroundColor Green
    }
} catch {
    Write-Host "后台服务启动失败: $($_.Exception.Message)" -ForegroundColor Red
    foreach ($process in $startedProcesses) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
