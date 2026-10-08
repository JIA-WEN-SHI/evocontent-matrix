param(
  [switch]$Bootstrap,
  [switch]$SeedDemo,
  [switch]$OpenBrowser,
  [switch]$NoMcp,
  [switch]$OpenMcpLogin
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDir = Join-Path $root "logs"
$runDir = Join-Path $root ".run"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$webNextDir = Join-Path $root "apps\web\.next"
$bootstrapPythonCandidates = @(
  "C:\Program Files\Python312\python.exe",
  "C:\Python312\python.exe"
)
$npmCandidates = @(
  "C:\Program Files\nodejs\npm.cmd",
  (Join-Path $root ".tools\node-v22.13.1-win-x64\npm.cmd")
)

function Resolve-PortableNpmCmd {
  $toolsRoot = Join-Path $root ".tools"
  if (-not (Test-Path $toolsRoot)) {
    return ""
  }
  $matches = Get-ChildItem -Path $toolsRoot -Recurse -File -Filter "npm.cmd" -ErrorAction SilentlyContinue
  if ($matches -and $matches.Count -gt 0) {
    return $matches[0].FullName
  }
  return ""
}

function Ensure-LocalTools {
  $npmExists = $false
  foreach ($candidate in $npmCandidates) {
    if (Test-Path $candidate) {
      $npmExists = $true
      break
    }
  }
  if (-not $npmExists) {
    $cmdNpm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($cmdNpm) {
      $npmExists = $true
    }
  }

  $gitExists = Test-Path "C:\Program Files\Git\cmd\git.exe"
  if (-not $gitExists) {
    $cmdGit = Get-Command git -ErrorAction SilentlyContinue
    if ($cmdGit) {
      $gitExists = $true
    }
  }

  if ($npmExists -and $gitExists) {
    return
  }

  Write-Warning "Missing required local tools (node/npm/git). Auto-installing portable toolchain..."
  $setupScript = Join-Path $PSScriptRoot "setup_portable_dev_env.ps1"
  if (-not (Test-Path $setupScript)) {
    throw "Missing setup script: $setupScript"
  }
  & $setupScript -ToolsRoot (Join-Path $root ".tools")
}

function Resolve-BootstrapPython {
  foreach ($candidate in $bootstrapPythonCandidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  $cmd = Get-Command py -ErrorAction SilentlyContinue
  if ($cmd) {
    return "py"
  }
  throw "Python executable not found. Install Python 3.12+ first."
}

function Resolve-NpmCmd {
  $portableNpm = Resolve-PortableNpmCmd
  if ($portableNpm) {
    return $portableNpm
  }
  foreach ($candidate in $npmCandidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if ($cmd) {
    return "npm.cmd"
  }
  throw "npm.cmd not found. Install Node.js first."
}

function Get-DotEnvValue {
  param(
    [string]$Key
  )
  $envFile = Join-Path $root ".env"
  if (-not (Test-Path $envFile)) { return "" }
  foreach ($rawLine in (Get-Content $envFile)) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
    $parts = $line.Split("=", 2)
    if ($parts[0].Trim() -ne $Key) { continue }
    $value = $parts[1].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
      if ($value.Length -ge 2) {
        $value = $value.Substring(1, $value.Length - 2)
      }
    }
    return $value
  }
  return ""
}

function Parse-Bool {
  param(
    [string]$Value,
    [bool]$Default = $false
  )
  $raw = ""
  if ($null -ne $Value) { $raw = [string]$Value }
  $v = $raw.Trim().ToLowerInvariant()
  if (-not $v) { return $Default }
  if ($v -in @("1", "true", "yes", "on")) { return $true }
  if ($v -in @("0", "false", "no", "off")) { return $false }
  return $Default
}

function Try-StartMcp {
  if ($NoMcp) {
    Write-Host "Skip MCP startup (-NoMcp)."
    return
  }
  $mcpEnabled = Parse-Bool (Get-DotEnvValue "XHS_MCP_READONLY_ENABLED") $false
  $mcpBaseUrl = (Get-DotEnvValue "XHS_MCP_BASE_URL").Trim()
  if (-not $mcpEnabled -or -not $mcpBaseUrl) {
    Write-Host "MCP startup skipped (XHS_MCP_READONLY not enabled or base_url empty)."
    return
  }

  $uri = $null
  try {
    $uri = [System.Uri]$mcpBaseUrl
  } catch {
    Write-Warning "Invalid XHS_MCP_BASE_URL: $mcpBaseUrl"
    return
  }
  if ($uri.Host -notin @("127.0.0.1", "localhost")) {
    Write-Host "MCP base_url points to remote host ($($uri.Host)); skip local MCP startup."
    return
  }

  $port = if ($uri.Port -gt 0) { [int]$uri.Port } else { 18060 }
  $installDirFromEnv = (Get-DotEnvValue "XHS_MCP_INSTALL_DIR").Trim()
  $installCandidates = @()
  if ($installDirFromEnv) { $installCandidates += $installDirFromEnv }
  $installCandidates += @(
    "D:\tools\xhs-mcp\xiaohongshu-mcp-windows-amd64",
    "D:\tools\xhs-mcp"
  )

  $resolvedInstallDir = ""
  foreach ($candidate in $installCandidates) {
    $exe = Join-Path $candidate "xiaohongshu-mcp-windows-amd64.exe"
    if (Test-Path $exe) {
      $resolvedInstallDir = $candidate
      break
    }
  }
  if (-not $resolvedInstallDir) {
    Write-Warning "MCP enabled but executable not found. Run scripts/install_xhs_mcp.ps1 first."
    return
  }

  $startScript = Join-Path $PSScriptRoot "start_xhs_mcp.ps1"
  if (-not (Test-Path $startScript)) {
    Write-Warning "Missing script: $startScript"
    return
  }

  Write-Host "Ensuring MCP is up (port $port)..."
  $args = @(
    "-ExecutionPolicy", "Bypass",
    "-File", $startScript,
    "-InstallDir", $resolvedInstallDir,
    "-Port", "$port"
  )
  if ($OpenMcpLogin) { $args += "-OpenLogin" }
  Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Hidden | Out-Null

  $ok = $false
  $start = Get-Date
  while (((Get-Date) - $start).TotalSeconds -lt 20) {
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $port }
    if ($listeners) {
      $ok = $true
      break
    }
    Start-Sleep -Milliseconds 500
  }
  if ($ok) {
    Write-Host "MCP ready at $mcpBaseUrl"
  } else {
    Write-Warning "MCP port $port did not become ready. Run scripts/start_xhs_mcp.ps1 manually."
  }
}

$bootstrapPython = Resolve-BootstrapPython
$npmCmd = Resolve-NpmCmd
$nodeDir = Split-Path $npmCmd -Parent
if ($nodeDir -and -not ($env:Path -split ';' | Where-Object { $_ -eq $nodeDir })) {
  $env:Path = "$nodeDir;$env:Path"
}

function Wait-Url {
  param(
    [string]$Url,
    [int]$TimeoutSec = 90
  )
  $start = Get-Date
  while (((Get-Date) - $start).TotalSeconds -lt $TimeoutSec) {
    try {
      $res = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
      if ($res.StatusCode -ge 200 -and $res.StatusCode -lt 500) {
        return $true
      }
    } catch {
      Start-Sleep -Milliseconds 700
    }
  }
  return $false
}

function Ensure-Bootstrap {
  if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..."
    if ($bootstrapPython -eq "py") {
      & py -3 -m venv (Join-Path $root ".venv")
    } else {
      & $bootstrapPython -m venv (Join-Path $root ".venv")
    }
  }

  if ($Bootstrap) {
    Write-Host "Installing Python dependencies..."
    & $venvPython -m pip install -U pip
    & $venvPython -m pip install -e (Join-Path $root "services/api") -e (Join-Path $root "services/agent")
    Write-Host "Installing Node dependencies..."
    Push-Location $root
    & $npmCmd install
    Pop-Location
  } else {
    if (-not (Test-Path (Join-Path $root "node_modules"))) {
      Write-Host "node_modules missing, running npm install..."
      Push-Location $root
      & $npmCmd install
      Pop-Location
    }
  }
}

function Stop-ListeningPort {
  param([int]$Port)
  $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($listener in $listeners) {
    $processId = [int]$listener.OwningProcess
    if ($processId -le 0) { continue }
    try {
      Stop-Process -Id $processId -Force -ErrorAction Stop
      Write-Host "Freed occupied port $Port (PID $processId)."
    } catch {
      Write-Warning "Failed to stop PID $processId on port $Port."
    }
  }
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
if (-not (Test-Path $runDir)) { New-Item -ItemType Directory -Force $runDir | Out-Null }

if (-not (Test-Path (Join-Path $root ".env"))) {
  Copy-Item (Join-Path $root ".env.example") (Join-Path $root ".env")
  Write-Warning ".env was missing; copied from .env.example. Please update secrets as needed."
}

Ensure-LocalTools

Ensure-Bootstrap

# stop any previously tracked processes first
& (Join-Path $PSScriptRoot "dev_down.ps1") -Quiet

# clear orphan listeners that may not be tracked in .run metadata
Stop-ListeningPort -Port 8000
Stop-ListeningPort -Port 8100
Stop-ListeningPort -Port 3001

# clear Next.js build cache to avoid stale chunk/runtime issues
if (Test-Path $webNextDir) {
  Remove-Item -Recurse -Force $webNextDir
}

Write-Host "Starting API..."
$api = Start-Process -FilePath $venvPython `
  -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000","--app-dir","services/api" `
  -WorkingDirectory $root `
  -RedirectStandardOutput (Join-Path $logDir "api.out.log") `
  -RedirectStandardError (Join-Path $logDir "api.err.log") `
  -WindowStyle Hidden `
  -PassThru
Set-Content (Join-Path $runDir "api.pid") $api.Id

Write-Host "Starting Agent..."
$agent = Start-Process -FilePath $venvPython `
  -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8100","--app-dir","services/agent" `
  -WorkingDirectory $root `
  -RedirectStandardOutput (Join-Path $logDir "agent.out.log") `
  -RedirectStandardError (Join-Path $logDir "agent.err.log") `
  -WindowStyle Hidden `
  -PassThru
Set-Content (Join-Path $runDir "agent.pid") $agent.Id

Write-Host "Starting Web..."
$env:NEXT_DISABLE_DEVTOOLS = "1"
$web = Start-Process -FilePath $npmCmd `
  -ArgumentList "run","dev","--","--hostname","127.0.0.1" `
  -WorkingDirectory (Join-Path $root "apps/web") `
  -RedirectStandardOutput (Join-Path $logDir "web.out.log") `
  -RedirectStandardError (Join-Path $logDir "web.err.log") `
  -WindowStyle Hidden `
  -PassThru
Set-Content (Join-Path $runDir "web.pid") $web.Id

if (-not (Wait-Url -Url "http://127.0.0.1:8000/health" -TimeoutSec 90)) {
  throw "API health check failed. See logs/api.err.log"
}
if (-not (Wait-Url -Url "http://127.0.0.1:8100/health" -TimeoutSec 90)) {
  throw "Agent health check failed. See logs/agent.err.log"
}
if (-not (Wait-Url -Url "http://127.0.0.1:3001/dashboard" -TimeoutSec 180)) {
  throw "Web health check failed. See logs/web.err.log"
}

Try-StartMcp

if ($SeedDemo) {
  if ($env:SUPABASE_MANAGEMENT_TOKEN -and $env:SUPABASE_PROJECT_REF) {
    Write-Host "Seeding demo runtime data..."
    & $venvPython (Join-Path $root "scripts/seed_demo_runtime.py")
  } else {
    Write-Warning "SeedDemo requested but SUPABASE_MANAGEMENT_TOKEN / SUPABASE_PROJECT_REF not set."
  }
}

Write-Host ""
Write-Host "Dev stack is up:"
Write-Host "  API:    http://127.0.0.1:8000/health"
Write-Host "  Agent:  http://127.0.0.1:8100/health"
Write-Host "  Web:    http://127.0.0.1:3001/dashboard"
Write-Host "  MCP:    auto-start (if XHS_MCP_READONLY_ENABLED=true)"
Write-Host ""
Write-Host "Logs:"
Write-Host "  logs/api.err.log"
Write-Host "  logs/agent.err.log"
Write-Host "  logs/web.err.log"
Write-Host ""
Write-Host "Stop with: powershell -ExecutionPolicy Bypass -File scripts/dev_down.ps1"
Write-Host "Optional:  -NoMcp (skip MCP), -OpenMcpLogin (open login tool)"

if ($OpenBrowser) {
  $url = "http://127.0.0.1:3001/dashboard"
  $opened = $false
  try {
    Start-Process $url | Out-Null
    $opened = $true
  } catch {}
  if (-not $opened) {
    try {
      Start-Process -FilePath "cmd.exe" -ArgumentList "/c","start","",$url -WindowStyle Hidden | Out-Null
      $opened = $true
    } catch {}
  }
  if (-not $opened) {
    try {
      Start-Process -FilePath "explorer.exe" -ArgumentList $url | Out-Null
      $opened = $true
    } catch {}
  }
  if ($opened) {
    Write-Host "Opened browser at /dashboard"
  } else {
    Write-Warning "Failed to auto-open browser. Open manually: $url"
  }
}
