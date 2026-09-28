param([switch]$SkipBuild, [switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$buildRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'pyACCS\windows_installer_work'
if (-not (Test-Path -LiteralPath $buildRoot)) { throw "Installer build workspace not found: $buildRoot" }
$arguments = @()
if ($SkipBuild) { $arguments += '-SkipBuild' }
if ($SkipInstaller) { $arguments += '-SkipInstaller' }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $buildRoot 'build_windows_installer.ps1') @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$release = Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Force -Path $release | Out-Null
Copy-Item (Join-Path $buildRoot 'release\pyPTA-Setup-1.0.1-Open-x64.exe'),
          (Join-Path $buildRoot 'release\pyPTA-Setup-1.0.1-Obfuscated-x64.exe'),
          (Join-Path $buildRoot 'release\pyPTA-Source-1.0.1.zip') -Destination $release -Force
Get-ChildItem $release -File | ForEach-Object {
    [pscustomobject]@{ Name=$_.Name; Bytes=$_.Length; SHA256=(Get-FileHash $_.FullName -Algorithm SHA256).Hash }
} | Format-Table -AutoSize
