param(
  [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runDir = Join-Path $root ".run"

function Stop-ListeningPort {
  param([int]$Port)
  $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($listener in $listeners) {
    $processId = [int]$listener.OwningProcess
    if ($processId -le 0) { continue }
    try {
      Stop-Process -Id $processId -Force -ErrorAction Stop
      if (-not $Quiet) { Write-Host "Stopped listener on port $Port -> PID $processId" }
    } catch {
      if (-not $Quiet) { Write-Host "Port $Port PID $processId already stopped." }
    }
  }
}

if (-not (Test-Path $runDir)) {
  if (-not $Quiet) { Write-Host "No running metadata found (.run missing)." }
  exit 0
}

$pidFiles = @("api.pid", "agent.pid", "web.pid")
foreach ($f in $pidFiles) {
  $p = Join-Path $runDir $f
  if (Test-Path $p) {
    $id = (Get-Content -Raw $p).Trim()
    if ($id) {
      try {
        Stop-Process -Id ([int]$id) -Force
        if (-not $Quiet) { Write-Host "Stopped $f -> PID $id" }
      } catch {
        if (-not $Quiet) { Write-Host "PID $id already stopped." }
      }
    }
    Remove-Item $p -Force
  }
}

# fallback cleanup for any orphan listeners
Stop-ListeningPort -Port 8000
Stop-ListeningPort -Port 8100
Stop-ListeningPort -Port 3001

if (-not $Quiet) { Write-Host "All tracked dev processes stopped." }
