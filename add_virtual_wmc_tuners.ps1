$ErrorActionPreference = 'Stop'

$base = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Media Center\Service\Video\Tuners\{71985F48-1CA1-11D3-9CC8-00C04F7971E0}'
$sourceName = 'Silicondust HDHomeRun Tuner 1010C032-0'
$source = Get-ChildItem $base |
    Where-Object { (Get-ItemProperty $_.PSPath).DevName -eq $sourceName } |
    Select-Object -First 1

if (-not $source) {
    throw "Source WMC tuner cache entry not found: $sourceName"
}

foreach ($target in @('104FFFF2-0', '104FFFF2-1')) {
    $existing = Get-ChildItem $base |
        Where-Object { (Get-ItemProperty $_.PSPath).DevName -eq "Silicondust HDHomeRun Tuner $target" } |
        Select-Object -First 1

    if ($existing) {
        Remove-Item $existing.PSPath -Recurse -Force
    }

    $guid = [guid]::NewGuid().ToString('B').ToUpper()
    $dest = Join-Path $base $guid
    Copy-Item $source.PSPath $dest -Recurse -Force
    Set-ItemProperty -Path $dest -Name DevName -Value "Silicondust HDHomeRun Tuner $target"
    Set-ItemProperty -Path $dest -Name DevPath -Value "Silicondust HDHomeRun Tuner $target"
    Set-ItemProperty -Path (Join-Path $dest 'UserSettings') -Name Enabled -Value 1 -ErrorAction SilentlyContinue
    Write-Host "Created WMC tuner cache entry $guid for $target"
}
