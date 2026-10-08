param(
  [string]$Query = "japan immigration",
  [int]$Limit = 5,
  [switch]$SkipSync
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$headers = @{
  "x-user-id" = "demo-reviewer"
  "x-user-role" = "admin"
  "Content-Type" = "application/json"
}

Write-Host "== MCP Readonly Status ==" -ForegroundColor Cyan
try {
  $status = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ops/mcp-readonly/status" -Headers $headers -Method Get
  $status | ConvertTo-Json -Depth 8
} catch {
  Write-Host "Status check failed: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}

Write-Host "`n== MCP Readonly Search ==" -ForegroundColor Cyan
try {
  $searchBody = @{
    query = $Query
    limit = $Limit
    source_kind = "hotspot"
  } | ConvertTo-Json -Depth 6 -Compress
  $searchBytes = [System.Text.Encoding]::UTF8.GetBytes($searchBody)
  $search = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ops/mcp-readonly/search" -Headers $headers -Method Post -ContentType "application/json; charset=utf-8" -Body $searchBytes
  $search | ConvertTo-Json -Depth 8
} catch {
  Write-Host "Search failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "`n== MCP Readonly Collect Intel ==" -ForegroundColor Cyan
try {
  $collectBody = @{
    domain_slug = "japan_immigration"
    query = $Query
    limit = [Math]::Max(1, [Math]::Min(50, $Limit * 2))
    include_home = $true
    include_search = $true
    include_detail_metrics = $false
    persist = $true
  } | ConvertTo-Json -Depth 6 -Compress
  $collectBytes = [System.Text.Encoding]::UTF8.GetBytes($collectBody)
  $collect = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ops/mcp-readonly/collect-intel" -Headers $headers -Method Post -ContentType "application/json; charset=utf-8" -Body $collectBytes
  $collect | ConvertTo-Json -Depth 8
} catch {
  Write-Host "Collect intel failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

if (-not $SkipSync) {
  Write-Host "`n== MCP Readonly Sync Metrics ==" -ForegroundColor Cyan
  try {
    $syncBody = @{
      domain_slug = "japan_immigration"
      limit = 10
    } | ConvertTo-Json -Depth 6 -Compress
    $syncBytes = [System.Text.Encoding]::UTF8.GetBytes($syncBody)
    $sync = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ops/mcp-readonly/sync-metrics" -Headers $headers -Method Post -ContentType "application/json; charset=utf-8" -Body $syncBytes
    $sync | ConvertTo-Json -Depth 8
  } catch {
    Write-Host "Sync metrics failed: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}
