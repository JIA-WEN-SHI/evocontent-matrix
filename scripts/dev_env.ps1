$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$nodeCandidates = @(
  "C:\Program Files\nodejs",
  (Join-Path $projectRoot ".tools\node-v22.13.1-win-x64")
)
$gitCandidates = @(
  (Join-Path $projectRoot ".tools\mingit\cmd")
)

foreach ($nodeDir in $nodeCandidates) {
  if (Test-Path $nodeDir) {
    if (-not ($env:PATH -split ";" | Where-Object { $_ -eq $nodeDir })) {
      $env:PATH = "$nodeDir;$env:PATH"
    }
  }
}
foreach ($gitDir in $gitCandidates) {
  if (Test-Path $gitDir) {
    if (-not ($env:PATH -split ";" | Where-Object { $_ -eq $gitDir })) {
      $env:PATH = "$gitDir;$env:PATH"
    }
  }
}

if (Test-Path $venvPython) {
  Write-Host "[dev-env] Python: $(& $venvPython --version)"
} else {
  Write-Warning "[dev-env] .venv Python not found: $venvPython"
}

if (Get-Command npm -ErrorAction SilentlyContinue) {
  Write-Host "[dev-env] npm: $(npm -v)"
} else {
  Write-Warning "[dev-env] npm not found in PATH"
}

if (Get-Command git -ErrorAction SilentlyContinue) {
  Write-Host "[dev-env] git: $(git --version)"
} else {
  Write-Warning "[dev-env] git not found in PATH"
}

Write-Host "[dev-env] Environment is ready for this shell session."
