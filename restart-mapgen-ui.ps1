<#
Restart the MapGen UI from this checkout without leaving an old server on 5001.

Run from the Thematic directory:
  powershell -ExecutionPolicy Bypass -File .\restart-mapgen-ui.ps1

The server stays attached to this terminal. Press Ctrl+C to stop it normally.
#>

[CmdletBinding()]
param(
    [int]$Port = 5001
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv_local\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "MapGen's virtual-environment Python was not found: $python"
}

function Get-ServerProcess([int]$TargetProcessId) {
    # Do not name this parameter $ProcessId: PowerShell treats that name as
    # the built-in, read-only $PID variable and resolves it to this launcher
    # process rather than the listener we are trying to inspect.
    Get-CimInstance Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction SilentlyContinue
}

function Is-MapGenServer($Process) {
    if ($null -eq $Process) { return $false }
    $command = [string]$Process.CommandLine
    return $command -match '(?i)webui[\\/]server\.py'
}

# A prior launch can survive a terminal/IDE restart. Only stop the listener if
# it is actually MapGen, then include MapGen parent processes (Werkzeug or an
# IDE wrapper can otherwise immediately leave another child holding the port).
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$toStop = [System.Collections.Generic.HashSet[int]]::new()
foreach ($listener in $listeners) {
    $process = Get-ServerProcess $listener.OwningProcess
    if (-not (Is-MapGenServer $process)) {
        throw "Port $Port is used by PID $($listener.OwningProcess), which is not MapGen. Refusing to stop it."
    }
    [void]$toStop.Add([int]$process.ProcessId)
    $parentId = [int]$process.ParentProcessId
    while ($parentId -gt 0) {
        $parent = Get-ServerProcess $parentId
        if (-not (Is-MapGenServer $parent)) { break }
        [void]$toStop.Add([int]$parent.ProcessId)
        $parentId = [int]$parent.ParentProcessId
    }
}

foreach ($processId in @($toStop | Sort-Object -Descending)) {
    Write-Host "Stopping existing MapGen server (PID $processId)..."
    Stop-Process -Id $processId -Force -ErrorAction Stop
}

if ($toStop.Count) {
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 150
        $stillListening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    } while ($stillListening -and (Get-Date) -lt $deadline)
    if ($stillListening) {
        throw "MapGen did not release port $Port. Close the process shown by: Get-NetTCPConnection -LocalPort $Port -State Listen"
    }
}

Set-Location -LiteralPath $projectRoot
Write-Host "Starting the current MapGen checkout on http://127.0.0.1:$Port"
$env:MAPGEN_PORT = [string]$Port
& $python .\webui\server.py
