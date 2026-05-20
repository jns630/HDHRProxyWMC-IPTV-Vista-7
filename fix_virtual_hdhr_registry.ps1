$paths = @(
    'HKLM:\SOFTWARE\Silicondust\HDHomeRun\Tuners\104FFFF2',
    'HKLM:\SOFTWARE\Silicondust\HDHomeRun\Tuners\104FFFF2-0',
    'HKLM:\SOFTWARE\Silicondust\HDHomeRun\Tuners\104FFFF2-1',
    'HKLM:\SOFTWARE\WOW6432Node\Silicondust\HDHomeRun\Tuners\104FFFF2',
    'HKLM:\SOFTWARE\WOW6432Node\Silicondust\HDHomeRun\Tuners\104FFFF2-0',
    'HKLM:\SOFTWARE\WOW6432Node\Silicondust\HDHomeRun\Tuners\104FFFF2-1'
)

foreach ($path in $paths) {
    if (Test-Path $path) {
        Set-ItemProperty -Path $path -Name Model -Value 'hdhomerun4_atsc'
        Set-ItemProperty -Path $path -Name SourceType -Value 'Digital Antenna'
        Set-ItemProperty -Path $path -Name Source -Value 'Digital Antenna'
    }
}
