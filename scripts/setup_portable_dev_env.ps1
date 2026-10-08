param(
  [string]$ToolsRoot = "D:\tools\portable"
)

$ErrorActionPreference = "Stop"

function Add-PathIfMissing {
  param(
    [string]$PathValue
  )
  if ([string]::IsNullOrWhiteSpace($PathValue)) {
    return
  }

  $normalized = $PathValue.Trim()
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $machinePath) {
    $machinePath = ""
  }
  $parts = $machinePath.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)
  $exists = $false
  foreach ($part in $parts) {
    if ($part.Trim().ToLowerInvariant() -eq $normalized.ToLowerInvariant()) {
      $exists = $true
      break
    }
  }

  if (-not $exists) {
    $newValue = if ($machinePath.Trim()) { "$machinePath;$normalized" } else { $normalized }
    [Environment]::SetEnvironmentVariable("Path", $newValue, "User")
    Write-Host "Added to User PATH: $normalized"
  } else {
    Write-Host "Already in User PATH: $normalized"
  }
}

function Ensure-Dir {
  param([string]$PathText)
  if (-not (Test-Path -LiteralPath $PathText)) {
    New-Item -ItemType Directory -Path $PathText -Force | Out-Null
  }
}

Ensure-Dir $ToolsRoot
$downloadRoot = Join-Path $PSScriptRoot "..\.run\install"
Ensure-Dir $downloadRoot

Write-Host "== Install MinGit (portable) =="
$gitRelease = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest"
$gitAsset = $gitRelease.assets | Where-Object { $_.name -match "^MinGit-.*-64-bit\.zip$" } | Select-Object -First 1
if (-not $gitAsset) {
  throw "Cannot find MinGit zip asset from git-for-windows latest release."
}
$gitZip = Join-Path $downloadRoot $gitAsset.name
Invoke-WebRequest -Uri $gitAsset.browser_download_url -OutFile $gitZip
$gitDest = Join-Path $ToolsRoot "git"
if (Test-Path -LiteralPath $gitDest) {
  Remove-Item -Recurse -Force -LiteralPath $gitDest
}
Expand-Archive -Path $gitZip -DestinationPath $gitDest -Force
$gitCmd = Join-Path $gitDest "cmd"
if (-not (Test-Path -LiteralPath (Join-Path $gitCmd "git.exe"))) {
  throw "MinGit extracted but git.exe not found under cmd."
}

Write-Host "== Install Node.js LTS (portable) =="
$nodeIndex = Invoke-RestMethod "https://nodejs.org/dist/index.json"
$nodeRelease = $nodeIndex |
  Where-Object { $_.lts -and $_.files -contains "win-x64-zip" } |
  Select-Object -First 1
if (-not $nodeRelease) {
  throw "Cannot resolve Node.js LTS win-x64-zip release."
}
$nodeVersion = [string]$nodeRelease.version
$nodeFileName = "node-$nodeVersion-win-x64.zip"
$nodeUrl = "https://nodejs.org/dist/$nodeVersion/$nodeFileName"
$nodeZip = Join-Path $downloadRoot $nodeFileName
Invoke-WebRequest -Uri $nodeUrl -OutFile $nodeZip

$nodeBase = Join-Path $ToolsRoot "node"
if (Test-Path -LiteralPath $nodeBase) {
  Remove-Item -Recurse -Force -LiteralPath $nodeBase
}
Ensure-Dir $nodeBase
Expand-Archive -Path $nodeZip -DestinationPath $nodeBase -Force
$nodeRoot = Join-Path $nodeBase "node-$nodeVersion-win-x64"
if (-not (Test-Path -LiteralPath (Join-Path $nodeRoot "node.exe"))) {
  throw "Node extracted but node.exe not found."
}
if (-not (Test-Path -LiteralPath (Join-Path $nodeRoot "npm.cmd"))) {
  throw "Node extracted but npm.cmd not found."
}

Write-Host "== Update PATH =="
Add-PathIfMissing -PathValue $gitCmd
Add-PathIfMissing -PathValue $nodeRoot

# Current process immediate effect
$env:Path = "$gitCmd;$nodeRoot;$env:Path"

Write-Host "== Verify =="
& (Join-Path $gitCmd "git.exe") --version
& (Join-Path $nodeRoot "node.exe") -v
& (Join-Path $nodeRoot "npm.cmd") -v

Write-Host ""
Write-Host "Portable dev environment ready."
Write-Host "If current terminal still cannot find commands, run: powershell -ExecutionPolicy Bypass -File scripts/refresh_shell_env.ps1"
