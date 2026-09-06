#Requires -Version 5
<#
.SYNOPSIS
  Start (or restart) the local-high stack: scanner, web dashboard, Cloudflare tunnel.

.DESCRIPTION
  Kills any running instances first, then starts all three hidden in the
  background and prints the dashboard URL. The Cloudflare quick tunnel gets a
  new random *.trycloudflare.com URL every time it restarts - this script
  reads it back out of logs\tunnel.log for you.

  Needs three user environment variables (set them once, they survive reboots):
    LOCAL_HIGH_TELEGRAM_BOT_TOKEN   - Telegram alerts
    LOCAL_HIGH_TELEGRAM_CHAT_ID     - Telegram alerts
    LOCAL_HIGH_WEB_TOKEN            - dashboard access token

  Usage:  right-click -> Run with PowerShell,  or:  powershell -File scripts\start.ps1
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv\Scripts\python.exe'
$cf   = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

foreach ($v in 'LOCAL_HIGH_TELEGRAM_BOT_TOKEN', 'LOCAL_HIGH_TELEGRAM_CHAT_ID', 'LOCAL_HIGH_WEB_TOKEN') {
    if (-not [Environment]::GetEnvironmentVariable($v, 'User')) {
        Write-Warning "$v is not set (User env var) - alerts / dashboard auth may not work."
    }
}
if (-not (Test-Path $py)) {
    throw "venv python not found at $py - run the install steps in README.md first."
}

Write-Host 'Stopping any running instances...' -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*local_high*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

Write-Host 'Starting scanner (Telegram alerts)...' -ForegroundColor Cyan
Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
    -ArgumentList '-m', 'local_high', '--config', 'config.yaml' `
    -RedirectStandardOutput (Join-Path $logs 'scanner.stdout.log') `
    -RedirectStandardError  (Join-Path $logs 'scanner.stderr.log')

Write-Host 'Starting web dashboard...' -ForegroundColor Cyan
Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
    -ArgumentList '-m', 'local_high.webapp', '--config', 'config.yaml' `
    -RedirectStandardOutput (Join-Path $logs 'webapp.stdout.log') `
    -RedirectStandardError  (Join-Path $logs 'webapp.stderr.log')

Start-Sleep -Seconds 3

$url = $null
if (Test-Path $cf) {
    Write-Host 'Starting Cloudflare tunnel...' -ForegroundColor Cyan
    $tunLog = Join-Path $logs 'tunnel.log'
    if (Test-Path $tunLog) { Remove-Item $tunLog -Force }
    Start-Process -FilePath $cf -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList 'tunnel', '--url', 'http://localhost:8787', '--logfile', $tunLog

    foreach ($i in 1..25) {
        Start-Sleep -Seconds 1
        if (Test-Path $tunLog) {
            $m = Select-String -Path $tunLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
                Select-Object -Last 1
            if ($m) { $url = $m.Matches[0].Value; break }
        }
    }
}
else {
    Write-Warning 'cloudflared not found - dashboard is local-only.'
}

$token = [Environment]::GetEnvironmentVariable('LOCAL_HIGH_WEB_TOKEN', 'User')
Write-Host ''
Write-Host '=== local-high is running ===' -ForegroundColor Green
if ($url) {
    Write-Host "Public:  $url/?token=$token" -ForegroundColor Green
}
else {
    Write-Host 'Public:  tunnel URL not found yet - check logs\tunnel.log in a moment.' -ForegroundColor Yellow
}
Write-Host "Local:   http://localhost:8787/?token=$token"
Write-Host ''
Write-Host 'Open the link once with ?token=... - it sets a cookie after that.'
Write-Host 'Stop everything with  scripts\stop.ps1'
Write-Host ''
Write-Host 'Closing this window is safe - the scanner, dashboard and tunnel keep' -ForegroundColor DarkGray
Write-Host 'running in the background. This window just tails the scanner log:' -ForegroundColor DarkGray
Write-Host '------------------------------------------------------------------------'

$scannerLog = Join-Path $logs 'scanner.stdout.log'
foreach ($i in 1..10) {
    if (Test-Path $scannerLog) { break }
    Start-Sleep -Seconds 1
}
if (Test-Path $scannerLog) {
    Get-Content -Path $scannerLog -Wait -Tail 30
}
else {
    Write-Host '(no scanner output yet - check logs\scanner.stderr.log)' -ForegroundColor Yellow
    Read-Host 'Press Enter to close'
}
