param(
    [ValidateRange(1, 65535)][int]$Port = 5000,
    [ValidateSet('auto', 'json', 'sqlite')][string]$StateBackend = 'auto',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'

function Test-DashboardPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync('0.0.0.0', $Port)
        return ($connection.Wait(500) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

$tailscale = (Get-Command tailscale.exe -CommandType Application -ErrorAction Stop).Source
if (-not $Python) {
    $localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { 'python.exe' }
}
$Python = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
if (Test-DashboardPort) {
    throw "Port $Port is already in use. Stop the existing dashboard before running this launcher."
}

$stdout = Join-Path $PSScriptRoot 'dashboard.log'
$stderr = Join-Path $PSScriptRoot 'dashboard.error.log'
$dashboard = Start-Process -FilePath $Python `
    -ArgumentList "-u adaptive_crypto_dashboard.py --host 0.0.0.0 --port $Port --state-backend $StateBackend" `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Host "Dashboard process: $($dashboard.Id). Logs: $stdout and $stderr"
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    if ($dashboard.HasExited) {
        throw "Dashboard exited with code $($dashboard.ExitCode). Check $stderr. Funnel was not enabled."
    }
    $ready = Test-DashboardPort
    if ($ready) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $ready -or $dashboard.HasExited) {
    throw "Dashboard did not start listening within 30 seconds. Check $stderr and process $($dashboard.Id). Funnel was not enabled."
}

& $tailscale funnel --bg "5000"
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale Funnel failed (exit $LASTEXITCODE). Dashboard remains running locally at http://127.0.0.1:$Port/."
}
Write-Host "Dashboard and Funnel are running in the background. You can close this terminal."
