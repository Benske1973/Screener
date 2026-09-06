#Requires -Version 5
<#
.SYNOPSIS
  Stop the local-high stack (scanner, web dashboard, Cloudflare tunnel).
#>
$ErrorActionPreference = 'Continue'

$py = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*local_high*' }
foreach ($p in $py) {
    Write-Host "stopping python PID $($p.ProcessId)  ($($p.CommandLine))"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "stopping cloudflared PID $($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

Write-Host 'stopped.'
