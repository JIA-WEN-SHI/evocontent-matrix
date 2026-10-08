$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$nodeDir = Join-Path $repoRoot ".tools\node-v22.13.1-win-x64"
$gitDir = Join-Path $repoRoot ".tools\mingit\cmd"

if (-not (Test-Path $nodeDir)) {
  throw "Node not found: $nodeDir"
}
if (-not (Test-Path $gitDir)) {
  throw "Git not found: $gitDir"
}

$env:PATH = "$nodeDir;$gitDir;$env:PATH"

Write-Host "Local tools enabled for current shell:"
Write-Host "  Node: $(node -v)"
Write-Host "  npm:  $(npm.cmd -v)"
Write-Host "  Git:  $(git --version)"
