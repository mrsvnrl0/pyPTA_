param(
    [string]$BindAddress = '0.0.0.0',
    [int]$Port = 5000,
    [ValidateSet('auto', 'json', 'sqlite')][string]$StateBackend = 'auto',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { 'python' }
}
& $Python .\adaptive_crypto_dashboard.py --host $BindAddress --port $Port --state-backend $StateBackend
exit $LASTEXITCODE
