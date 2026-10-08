param(
  [string]$InstallDir = "D:\tools\xhs-mcp\xiaohongshu-mcp-windows-amd64",
  [int]$Port = 18060,
  [switch]$OpenLogin
)

$ErrorActionPreference = "Stop"

$mcpExe = Join-Path $InstallDir "xiaohongshu-mcp-windows-amd64.exe"
$loginExe = Join-Path $InstallDir "xiaohongshu-login-windows-amd64.exe"
$stdoutLog = Join-Path $InstallDir "mcp.stdout.log"
$stderrLog = Join-Path $InstallDir "mcp.stderr.log"

if (-not (Test-Path $mcpExe)) {
  throw "未找到 MCP 可执行文件: $mcpExe"
}

if ($OpenLogin) {
  if (-not (Test-Path $loginExe)) {
    throw "未找到登录工具: $loginExe"
  }
  Write-Host "Opening login tool: $loginExe"
  Start-Process -FilePath $loginExe -WorkingDirectory $InstallDir | Out-Null
}

$existing = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $Port }
if ($existing) {
  Write-Host "Port $Port already listening. PID=$($existing.OwningProcess)"
} else {
  $args = @("-headless=false", "-port=:$Port")
  $proc = Start-Process -FilePath $mcpExe -ArgumentList $args -WorkingDirectory $InstallDir -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
  Start-Sleep -Seconds 2
  Write-Host "MCP started. PID=$($proc.Id)"
}

Write-Host ""
Write-Host "Port check:"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $Port } | Select-Object LocalAddress, LocalPort, OwningProcess, State | Format-Table -AutoSize

Write-Host ""
Write-Host "Recent logs:"
if (Test-Path $stdoutLog) {
  Write-Host "--- stdout ---"
  Get-Content $stdoutLog -Tail 20
}
if (Test-Path $stderrLog) {
  Write-Host "--- stderr ---"
  Get-Content $stderrLog -Tail 20
}
