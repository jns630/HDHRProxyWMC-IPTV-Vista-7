$ErrorActionPreference = 'Stop'

$base = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Media Center\Service\Video\Tuners\{71985F48-1CA1-11D3-9CC8-00C04F7971E0}'
$virtualPattern = '104FFFF2-[01]$'

Get-ChildItem $base | ForEach-Object {
    $props = Get-ItemProperty $_.PSPath
    if ($props.DevName -match '^Silicondust HDHomeRun Tuner ') {
        if ($props.DevName -match $virtualPattern) {
            Set-ItemProperty -Path $_.PSPath -Name Disabled -Type DWord -Value 0
            Write-Host "Enabled $($props.DevName)"
        }
        else {
            Set-ItemProperty -Path $_.PSPath -Name Disabled -Type DWord -Value 1
            Write-Host "Disabled $($props.DevName)"
        }
    }
}
