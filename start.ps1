param(
    [string]$Python = "python",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$HealthUrl = "http://$BindHost`:$Port/api/health"

try {
    $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
    if ($resp.ok -and $resp.service -eq "qinling-agc") {
        Write-Host "AGC 服务已在运行: http://$BindHost`:$Port/ (PID=$($resp.pid))"
        exit 0
    }
    throw "端口 $Port 已被其他服务占用"
} catch {
    if ($_.Exception.Message -like "*占用*") { throw }
}

$LogDir = Join-Path $Root "log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$OutLog = Join-Path $LogDir "agc_web.stdout.log"
$ErrLog = Join-Path $LogDir "agc_web.stderr.log"

Start-Process -FilePath $Python `
    -ArgumentList @("-X", "utf8", (Join-Path $Root "run_agc.py"), "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog | Out-Null

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 300
    try {
        $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        if ($resp.ok -and $resp.service -eq "qinling-agc") {
            Write-Host "AGC 服务启动成功: http://$BindHost`:$Port/ (PID=$($resp.pid))"
            exit 0
        }
    } catch {}
}

throw "AGC 服务启动超时，请检查日志: $ErrLog"
