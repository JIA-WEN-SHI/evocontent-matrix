$ErrorActionPreference = "Stop"

function Read-EnvMap {
  param([string]$Path)
  $map = @{}
  if (-not (Test-Path $Path)) { return $map }
  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    $map[$key] = $val
  }
  return $map
}

function Check-Url {
  param([string]$Url)
  try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
    return @{ ok = $true; status = $resp.StatusCode }
  } catch {
    return @{ ok = $false; status = $_.Exception.Message }
  }
}

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"
$envMap = Read-EnvMap -Path $envPath

Write-Host "== Local Env Check ==" -ForegroundColor Cyan
Write-Host ".env: $envPath"
if ($envMap.ContainsKey("SUPABASE_URL")) {
  $sbUrl = $envMap["SUPABASE_URL"]
  $ref = ""
  try {
    $uri = [Uri]$sbUrl
    $sbHost = $uri.Host
    if ($sbHost -match "^([^.]+)\.supabase\.co$") {
      $ref = $Matches[1]
    }
  } catch {
    $ref = ""
  }
  if ($ref) {
    Write-Host "supabase_ref: $ref"
  } else {
    Write-Host "supabase_ref: (unable to parse from SUPABASE_URL)"
  }
}

$api = Check-Url -Url "http://127.0.0.1:8000/health"
$agent = Check-Url -Url "http://127.0.0.1:8100/health"
Write-Host ("API(8000):   " + ($(if($api.ok){"OK"} else {"DOWN"})) + " - " + $api.status)
Write-Host ("Agent(8100): " + ($(if($agent.ok){"OK"} else {"DOWN"})) + " - " + $agent.status)

if ($api.ok) {
  try {
    $h = @{ "x-user-id" = "demo-reviewer"; "x-user-role" = "admin" }
    $ready = Invoke-RestMethod -Headers $h -Uri "http://127.0.0.1:8000/api/system/readiness"
    Write-Host "`n== System Readiness ==" -ForegroundColor Cyan
    Write-Host ("pipeline_mode: " + $ready.pipeline_mode)
    Write-Host ("core_ready: " + $ready.core_ready)
    Write-Host ("suggestions_count: " + (($ready.suggestions | Measure-Object).Count))
    Write-Host "Tip: open /assistant and run \"系统体检\" to view readable Chinese suggestions."
  } catch {
    Write-Warning "Read /api/system/readiness failed: $($_.Exception.Message)"
  }
}

Write-Host "`n== Key Variables ==" -ForegroundColor Cyan
$checks = @(
  @{ key = "SUPABASE_URL"; required = $true; hint = "Supabase project URL" },
  @{ key = "SUPABASE_SERVICE_ROLE_KEY"; required = $true; hint = "Supabase service role key" },
  @{ key = "OPENAI_API_KEY"; required = $true; hint = "Needed for real content generation" },
  @{ key = "PLAYWRIGHT_DRY_RUN"; required = $true; hint = "Set false for real publish" },
  @{ key = "PLAYWRIGHT_STORAGE_STATE_PATH"; required = $false; hint = "Browser login state file" },
  @{ key = "PLAYWRIGHT_PROXY_SERVER"; required = $false; hint = "Residential/static proxy server for crawler/publisher" },
  @{ key = "XHS_MCP_READONLY_ENABLED"; required = $false; hint = "Enable read-only MCP for search/metrics" },
  @{ key = "XHS_MCP_WRITE_ENABLED"; required = $false; hint = "Enable MCP write tools (publish/comment/like)" },
  @{ key = "XHS_MCP_BASE_URL"; required = $false; hint = "Readonly MCP server URL" },
  @{ key = "XHS_MCP_WRITE_TOOL_ALLOWLIST"; required = $false; hint = "Comma-separated MCP write tool allowlist" },
  @{ key = "XHS_MCP_FEED_TOOL"; required = $false; hint = "MCP tool for home feed retrieval" },
  @{ key = "XHS_MCP_SEARCH_TOOL"; required = $false; hint = "MCP tool for search retrieval" },
  @{ key = "XHS_MCP_METRICS_TOOL"; required = $false; hint = "MCP tool for post metrics" }
)

foreach ($item in $checks) {
  $value = ""
  if ($envMap.ContainsKey($item.key)) { $value = $envMap[$item.key] }
  $ok = [string]::IsNullOrWhiteSpace($value) -eq $false
  $status = ""
  $hint = $item.hint
  if ($item.key -eq "PLAYWRIGHT_DRY_RUN") {
    if ([string]::IsNullOrWhiteSpace($value)) {
      $status = "MISSING"
    } elseif ($value -match "^(false|0)$") {
      $status = "LIVE"
      $hint = "Real publish mode enabled"
    } elseif ($value -match "^(true|1)$") {
      $status = "DRY_RUN"
      $hint = "Simulation mode (no real publish)"
    } else {
      $status = "INVALID"
      $hint = "Use true/false"
    }
  } elseif ($item.key -eq "PLAYWRIGHT_STORAGE_STATE_PATH") {
    if ([string]::IsNullOrWhiteSpace($value)) {
      $status = "OPTIONAL"
      $hint = "Needed only for real publish"
    } else {
      $status = "OK"
    }
  } elseif ($item.key -eq "OPENAI_API_KEY") {
    if ([string]::IsNullOrWhiteSpace($value)) {
      $status = "MISSING"
    } else {
      $status = "OK"
    }
  } elseif ($item.key -eq "XHS_MCP_READONLY_ENABLED") {
    if ([string]::IsNullOrWhiteSpace($value)) {
      $status = "OPTIONAL"
      $hint = "Set true only when MCP read-only bridge is configured"
    } elseif ($value -match "^(true|1)$") {
      $status = "ENABLED"
      $hint = "Remember to set XHS_MCP_BASE_URL"
    } elseif ($value -match "^(false|0)$") {
      $status = "DISABLED"
      $hint = "Using built-in crawler and data sources"
    } else {
      $status = "INVALID"
      $hint = "Use true/false"
    }
  } elseif ($item.key -eq "XHS_MCP_WRITE_ENABLED") {
    if ([string]::IsNullOrWhiteSpace($value)) {
      $status = "OPTIONAL"
      $hint = "Set true only when write MCP tools are required"
    } elseif ($value -match "^(true|1)$") {
      $status = "ENABLED"
      $hint = "Write tools enabled; verify allowlist before use"
    } elseif ($value -match "^(false|0)$") {
      $status = "DISABLED"
      $hint = "Write tools disabled"
    } else {
      $status = "INVALID"
      $hint = "Use true/false"
    }
  } elseif ($item.key -eq "XHS_MCP_BASE_URL") {
    $mcpEnabled = (
      ($envMap.ContainsKey("XHS_MCP_READONLY_ENABLED") -and ($envMap["XHS_MCP_READONLY_ENABLED"] -match "^(true|1)$")) -or
      ($envMap.ContainsKey("XHS_MCP_WRITE_ENABLED") -and ($envMap["XHS_MCP_WRITE_ENABLED"] -match "^(true|1)$"))
    )
    if ($mcpEnabled -and [string]::IsNullOrWhiteSpace($value)) {
      $status = "MISSING"
      $hint = "Required when MCP readonly/write is enabled"
    } else {
      $status = if ([string]::IsNullOrWhiteSpace($value)) { "OPTIONAL" } else { "OK" }
    }
  } else {
    $status = if ($ok) { "OK" } else { if ($item.required) { "MISSING" } else { "OPTIONAL" } }
  }
  Write-Host ("{0,-28} {1,-8} {2}" -f $item.key, $status, $hint)
}

Write-Host "`n== Minimum Inputs You Need To Provide ==" -ForegroundColor Yellow
Write-Host "1) OPENAI_API_KEY (or Anthropic key)"
Write-Host "2) Real publish or not. If yes, set PLAYWRIGHT_DRY_RUN=false"
Write-Host "3) PLAYWRIGHT_STORAGE_STATE_PATH for a logged-in account"
Write-Host "4) Result/error text after running SQL migrations 003/004/005 (and 006 if using account management)"
