param(
  [string]$StatePath = ""
)

$ErrorActionPreference = "Stop"

function Read-EnvValue {
  param([string]$Path, [string]$Key)
  if (-not (Test-Path $Path)) { return "" }
  $line = Get-Content $Path | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
  if (-not $line) { return "" }
  return ($line -replace "^$Key=", "").Trim()
}

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"

if (-not $StatePath) {
  $StatePath = Read-EnvValue -Path $envPath -Key "PLAYWRIGHT_STORAGE_STATE_PATH"
}

if (-not $StatePath) {
  Write-Host "PLAYWRIGHT_STORAGE_STATE_PATH is empty." -ForegroundColor Yellow
  Write-Host "Set it in .env, then run this script again."
  exit 1
}

if (-not [System.IO.Path]::IsPathRooted($StatePath)) {
  $StatePath = Join-Path $root $StatePath
}

Write-Host "Checking: $StatePath"

if (-not (Test-Path $StatePath)) {
  Write-Host "File not found." -ForegroundColor Red
  exit 1
}

$raw = Get-Content -Raw $StatePath
try {
  $obj = $raw | ConvertFrom-Json
} catch {
  Write-Host "Invalid JSON format." -ForegroundColor Red
  exit 1
}

$cookiesCount = 0
$originsCount = 0
if ($obj.PSObject.Properties.Name -contains "cookies" -and $obj.cookies) {
  $cookiesCount = @($obj.cookies).Count
}
if ($obj.PSObject.Properties.Name -contains "origins" -and $obj.origins) {
  $originsCount = @($obj.origins).Count
}

Write-Host ("cookies: " + $cookiesCount)
Write-Host ("origins: " + $originsCount)

if ($cookiesCount -gt 0 -or $originsCount -gt 0) {
  Write-Host "Storage state looks usable." -ForegroundColor Green
  exit 0
}

Write-Host "Storage state is valid JSON but empty (no cookies/origins)." -ForegroundColor Yellow
exit 1
