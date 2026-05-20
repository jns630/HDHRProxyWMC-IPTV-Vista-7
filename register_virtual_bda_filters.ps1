$ErrorActionPreference = 'Stop'

$categories = @(
    'HKLM:\SOFTWARE\Classes\CLSID\{71985F48-1CA1-11D3-9CC8-00C04F7971E0}\Instance',
    'HKLM:\SOFTWARE\Classes\CLSID\{FD0A5AF4-B41D-11D2-9C95-00C04F7971E0}\Instance',
    'HKLM:\SOFTWARE\WOW6432Node\Classes\CLSID\{71985F48-1CA1-11D3-9CC8-00C04F7971E0}\Instance',
    'HKLM:\SOFTWARE\WOW6432Node\Classes\CLSID\{FD0A5AF4-B41D-11D2-9C95-00C04F7971E0}\Instance'
)

$sourceName = 'Silicondust HDHomeRun Tuner 1010C032-0'
$targets = @(
    'Silicondust HDHomeRun Tuner 104FFFF2-0',
    'Silicondust HDHomeRun Tuner 104FFFF2-1'
)

foreach ($category in $categories) {
    $source = Join-Path $category $sourceName
    if (-not (Test-Path $category)) {
        New-Item -Path $category -Force | Out-Null
    }
    if (-not (Test-Path $source)) {
        $fallback = $source -replace 'HKLM:\\SOFTWARE\\WOW6432Node\\Classes', 'HKLM:\SOFTWARE\Classes'
        if (Test-Path $fallback) {
            Copy-Item $fallback $source -Recurse -Force
        }
        else {
            throw "Missing source BDA filter instance: $source"
        }
    }

    foreach ($targetName in $targets) {
        $target = Join-Path $category $targetName
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force
        }

        Copy-Item $source $target -Recurse -Force
        Set-ItemProperty -Path $target -Name FriendlyName -Value $targetName
        Write-Host "Registered $targetName in $category"
    }
}
