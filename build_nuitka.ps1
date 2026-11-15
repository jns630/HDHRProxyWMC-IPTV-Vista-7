param(
    [string]$Python = "python",
    [ValidateSet("standalone", "onefile")]
    [string]$Mode = "standalone",
    [string]$OutputDir = "nuitka-build",
    [string]$OutputName = "HDHRProxyWMC-IPTV.exe",
    [switch]$UacAdmin,
    [string]$Compiler = "",
    [switch]$RemoveOutput
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$helper = Join-Path $ProjectRoot "vista_helper\bin\HDHRProxyWMC-VistaGuideImport.exe"
if (-not (Test-Path $helper)) {
    throw "Vista guide helper not found: $helper"
}

$nuitkaArgs = @(
    "-m", "nuitka",
    "--$Mode",
    "--assume-yes-for-downloads",
    "--include-data-files=vista_helper/bin/HDHRProxyWMC-VistaGuideImport.exe=HDHRProxyWMC-VistaGuideImport.exe",
    "--output-dir=$OutputDir",
    "--output-filename=$OutputName"
)

if ($UacAdmin) {
    $nuitkaArgs += "--windows-uac-admin"
}
if ($Compiler) {
    $nuitkaArgs += "--$Compiler"
}
if ($RemoveOutput) {
    $nuitkaArgs += "--remove-output"
}
$nuitkaArgs += "main.py"

Write-Host "Python:  $Python"
Write-Host "Mode:    $Mode"
Write-Host "Output:  $OutputDir\$OutputName"
Write-Host "Command: $Python $($nuitkaArgs -join ' ')"

& $Python @nuitkaArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
