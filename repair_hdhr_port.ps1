param()

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script in an elevated PowerShell window."
    exit 1
}

Write-Host "Stopping Windows NAT driver so reserved UDP ports can be released..."
Stop-Service winnat -Force -ErrorAction SilentlyContinue

try {
    netsh int ipv4 delete excludedportrange protocol=udp startport=65001 numberofports=1 | Out-Null
} catch {
}

netsh int ipv4 add excludedportrange protocol=udp startport=65001 numberofports=1 store=persistent
Start-Service winnat

Write-Host ""
Write-Host "UDP 65001 exclusions:"
netsh interface ipv4 show excludedportrange protocol=udp | Select-String -Pattern "^\s*65001\s+65001"

Write-Host ""
Write-Host "Repair complete. Restart HDHRProxy, then rescan tuners."
