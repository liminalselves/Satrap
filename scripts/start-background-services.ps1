# Satrap 后台服务生命周期脚本
# 负责清理并隐藏启动控制服务和聊天服务

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$StopOnly,
    [switch]$Detach,
    [switch]$StopFrontend
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

function Get-SatrapAuthHeaders {
    $token = $env:SATRAP_API_TOKEN
    if ([string]::IsNullOrWhiteSpace($token)) {
        $tokenPath = Join-Path $DataDir "api-token"
        if (Test-Path -LiteralPath $tokenPath) {
            $token = (Get-Content -Raw -Encoding UTF8 -LiteralPath $tokenPath).Trim()
        }
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        return @{}
    }
    return @{ Authorization = "Bearer $token" }
}

if ($Detach) {
    if ($StopFrontend) {
        $frontendPort = 5173
        $activePorts = @(
            [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
                Select-Object -ExpandProperty Port -Unique
        )
        if ($activePorts -contains $frontendPort) {
            $frontendRootPattern = [Regex]::Escape((Join-Path $ProjectRoot "satrap-ui"))
            $frontendOwnerIds = @(
                Get-NetTCPConnection -LocalPort $frontendPort -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique
            )
            foreach ($ownerId in $frontendOwnerIds) {
                $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
                $isSatrapVite = $null -ne $owner -and
                    $owner.Name -eq "node.exe" -and
                    $owner.CommandLine -match $frontendRootPattern -and
                    $owner.CommandLine -match "(?i)[\\/]vite[\\/]bin[\\/]vite\.js"
                if ($isSatrapVite) {
                    Stop-Process -Id $ownerId -Force -ErrorAction Stop
                }
            }
        }
    }

    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    $stdoutPath = Join-Path $DataDir "background-services.stdout.log"
    $stderrPath = Join-Path $DataDir "background-services.stderr.log"
    $argumentList = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$PSCommandPath`"",
        "-ProjectRoot",
        "`"$ProjectRoot`""
    )
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $argumentList `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath | Out-Null
    return
}

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

function Get-PythonProcesses {
    return @(
        Get-CimInstance Win32_Process `
            -Filter "Name LIKE 'python%.exe' OR Name = 'py.exe'" `
            -ErrorAction Stop
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
    param(
        [PSCustomObject]$Service,
        [object[]]$PythonProcesses,
        [int[]]$PortOwnerIds
    )

    $moduleProcesses = @(
        $PythonProcesses | Where-Object {
            Test-ModuleCommandLine -CommandLine $_.CommandLine -Module $Service.Module
        }
    )
    $moduleProcessIds = @($moduleProcesses | Select-Object -ExpandProperty ProcessId)
    $ownsPort = @($PortOwnerIds | Where-Object { $moduleProcessIds -contains $_ }).Count -gt 0

    if ($ownsPort -and $Service.StopBackend) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:19871/stop" -Method Post -Headers (Get-SatrapAuthHeaders) -TimeoutSec 5 | Out-Null
        } catch {
            Write-Host "  $($Service.Name)未能通过接口停止平台后端, 将继续清理服务进程" -ForegroundColor DarkYellow
        }
    }

    if ($ownsPort) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$($Service.Port)/shutdown" -Method Post -Headers (Get-SatrapAuthHeaders) -TimeoutSec 2 | Out-Null
        } catch {}
    }

    return $moduleProcesses
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
            Invoke-RestMethod -Uri $Service.HealthUrl -Method Get -Headers (Get-SatrapAuthHeaders) -TimeoutSec 5 | Out-Null
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    throw "$($Service.Name)健康检查超时: $($Service.HealthUrl)"
}

function Stop-BackgroundServices {
    $pythonProcesses = @(Get-PythonProcesses)
    $listeningConnections = @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $Services.Port -contains $_.LocalPort }
    )
    $serviceProcesses = @{}

    foreach ($service in $Services) {
        $portOwnerIds = @(
            $listeningConnections |
                Where-Object { $_.LocalPort -eq $service.Port } |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
        $serviceProcesses[$service.Module] = @(
            Stop-ServiceInstance `
                -Service $service `
                -PythonProcesses $pythonProcesses `
                -PortOwnerIds $portOwnerIds
        )
    }

    if (@($serviceProcesses.Values | ForEach-Object { $_ }).Count -gt 0) {
        Start-Sleep -Milliseconds 700
    }

    foreach ($service in $Services) {
        foreach ($process in $serviceProcesses[$service.Module]) {
            if ($null -eq (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
                continue
            }
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Host "  已停止$($service.Name), PID $($process.ProcessId)" -ForegroundColor DarkGray
        }
    }

    if (@($serviceProcesses.Values | ForEach-Object { $_ }).Count -gt 0) {
        Start-Sleep -Milliseconds 300
    }

    $activePorts = @(
        [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
            Select-Object -ExpandProperty Port -Unique
    )
    foreach ($service in $Services) {
        if ($activePorts -notcontains $service.Port) {
            continue
        }
        $remainingOwnerIds = @(Get-PortOwnerIds -Port $service.Port)
        $ownerDescriptions = foreach ($ownerId in $remainingOwnerIds) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
            if ($null -eq $owner) {
                "PID $ownerId"
            } else {
                "$($owner.Name), PID $ownerId"
            }
        }
        throw "$($service.Name)端口 $($service.Port) 被其他进程占用: $($ownerDescriptions -join ', ')"
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
    $startedServices = @()

    foreach ($service in $Services) {
        Write-Host "正在隐藏启动$($service.Name)..." -ForegroundColor Cyan
        $process = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @("-m", $service.Module) `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -PassThru
        $startedProcesses += $process
        $startedServices += [PSCustomObject]@{
            Service = $service
            Process = $process
        }
    }

    foreach ($started in $startedServices) {
        Wait-ServiceHealth -Service $started.Service -Process $started.Process
        Write-Host "  $($started.Service.Name)已就绪, PID $($started.Process.Id), 端口 $($started.Service.Port)" -ForegroundColor Green
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
