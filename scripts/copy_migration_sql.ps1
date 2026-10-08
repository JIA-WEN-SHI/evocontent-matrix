param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("003", "004", "005", "006", "007")]
  [string]$Migration
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$map = @{
  "003" = "infra/supabase/migrations/003_matrix_pipeline.sql"
  "004" = "infra/supabase/migrations/004_daily_ops_reports.sql"
  "005" = "infra/supabase/migrations/005_repair_japan_domain_text.sql"
  "006" = "infra/supabase/migrations/006_channel_accounts.sql"
  "007" = "infra/supabase/migrations/007_channel_accounts_credentials.sql"
}

$rel = $map[$Migration]
$path = Join-Path $root $rel

if (-not (Test-Path $path)) {
  Write-Error "Migration file not found: $path"
}

$sql = Get-Content -Raw $path
$sql | Set-Clipboard

Write-Host "Copied migration SQL to clipboard: $rel" -ForegroundColor Green
Write-Host "Now paste into Supabase SQL Editor and click Run."
