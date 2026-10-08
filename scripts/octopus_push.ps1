param(
    [Parameter(Mandatory = $false)]
    [string]$ApiBase = "http://127.0.0.1:8000",

    [Parameter(Mandatory = $false)]
    [string]$WebhookPath = "/api/webhooks/kb-octopus",

    [Parameter(Mandatory = $true)]
    [string]$PayloadFile,

    [Parameter(Mandatory = $true)]
    [string]$Secret,

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PayloadFile)) {
    throw "Payload file not found: $PayloadFile"
}

$raw = Get-Content -LiteralPath $PayloadFile -Raw -Encoding UTF8
if ([string]::IsNullOrWhiteSpace($raw)) {
    throw "Payload file is empty: $PayloadFile"
}

# Validate JSON early to avoid sending malformed body.
$null = $raw | ConvertFrom-Json

$bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
$key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
$hmac = [System.Security.Cryptography.HMACSHA256]::new($key)
try {
    $hash = $hmac.ComputeHash($bytes)
}
finally {
    $hmac.Dispose()
}

$signature = ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
$uri = "$($ApiBase.TrimEnd('/'))$WebhookPath"

Write-Host "Webhook URI  : $uri"
Write-Host "Payload file : $PayloadFile"
Write-Host "Signature    : $signature"

if ($DryRun) {
    Write-Host "DryRun enabled, request not sent."
    exit 0
}

$headers = @{
    "x-signature" = $signature
}

$resp = Invoke-RestMethod `
    -Method Post `
    -Uri $uri `
    -Headers $headers `
    -ContentType "application/json; charset=utf-8" `
    -Body $raw

Write-Host "Response:"
$resp | ConvertTo-Json -Depth 8
