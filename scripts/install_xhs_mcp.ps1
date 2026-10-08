param(
  [string]$InstallDir = "D:\tools\xhs-mcp",
  [string]$Repo = "xpzouying/xiaohongshu-mcp"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$latest = Invoke-WebRequest -Uri "https://github.com/$Repo/releases/latest" -MaximumRedirection 5
$latestUrl = $latest.BaseResponse.ResponseUri.AbsoluteUri
if ($latestUrl -notmatch "/releases/tag/([^/]+)$") {
  throw "无法解析 latest tag: $latestUrl"
}
$tag = $Matches[1]
$zipName = "xiaohongshu-mcp-windows-amd64.zip"
$zipUrl = "https://github.com/$Repo/releases/download/$tag/$zipName"
$zipPath = Join-Path $InstallDir $zipName
$extractDir = Join-Path $InstallDir "xiaohongshu-mcp-windows-amd64"

Write-Host "Downloading: $zipUrl"
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -MaximumRedirection 10

Write-Host "Extracting to: $extractDir"
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

Write-Host "Installed files:"
Get-ChildItem -Path $extractDir | Select-Object Name, Length | Format-Table -AutoSize
