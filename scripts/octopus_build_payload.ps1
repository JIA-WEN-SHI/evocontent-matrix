param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [Parameter(Mandatory = $true)]
    [string]$OutputFile,

    [Parameter(Mandatory = $false)]
    [ValidateSet("case", "asset", "user_need", "review")]
    [string]$EntityType = "case",

    [Parameter(Mandatory = $false)]
    [string]$DomainSlug = "japan_immigration",

    [Parameter(Mandatory = $false)]
    [string]$Source = "octopus",

    [Parameter(Mandatory = $false)]
    [string]$SourceRunId = "",

    [Parameter(Mandatory = $false)]
    [string]$DefaultPlatform = "xiaohongshu",

    [Parameter(Mandatory = $false)]
    [int]$MaxItems = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $InputFile)) {
    throw "Input file not found: $InputFile"
}

$rawText = Get-Content -LiteralPath $InputFile -Raw -Encoding UTF8
if ([string]::IsNullOrWhiteSpace($rawText)) {
    throw "Input file is empty: $InputFile"
}

$parsed = $rawText | ConvertFrom-Json

function Get-RowValue {
    param(
        [Parameter(Mandatory = $true)]$Row,
        [Parameter(Mandatory = $true)][string[]]$Candidates
    )
    foreach ($name in $Candidates) {
        $prop = $Row.PSObject.Properties[$name]
        if ($null -ne $prop -and $null -ne $prop.Value -and -not [string]::IsNullOrWhiteSpace([string]$prop.Value)) {
            return [string]$prop.Value
        }
    }
    return ""
}

function To-IntOrNull {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $n = 0
    if ([int]::TryParse($Value, [ref]$n)) { return $n }
    return $null
}

function To-TagsArray {
    param($Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Array]) {
        $arr = @()
        foreach ($v in $Value) {
            if ($null -ne $v -and -not [string]::IsNullOrWhiteSpace([string]$v)) {
                $arr += [string]$v
            }
        }
        return $arr
    }
    $s = [string]$Value
    if ([string]::IsNullOrWhiteSpace($s)) { return @() }
    return ($s -split "[,;|]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
}

$rows = @()
if ($parsed -is [System.Array]) {
    $rows = @($parsed)
}
else {
    $dataProp = $parsed.PSObject.Properties["data"]
    $itemsProp = $parsed.PSObject.Properties["items"]
    if ($null -ne $dataProp -and $dataProp.Value -is [System.Array]) {
        $rows = @($dataProp.Value)
    }
    elseif ($null -ne $itemsProp -and $itemsProp.Value -is [System.Array]) {
        $rows = @($itemsProp.Value)
    }
    else {
        throw "Unsupported JSON shape. Expected array or object with data/items array."
    }
}

if ($rows.Count -eq 0) {
    throw "No rows found in input JSON."
}

if ($rows.Count -gt $MaxItems) {
    Write-Warning "Input has $($rows.Count) rows; keeping first $MaxItems rows."
    $rows = $rows | Select-Object -First $MaxItems
}

if ([string]::IsNullOrWhiteSpace($SourceRunId)) {
    $SourceRunId = "octopus-run-" + (Get-Date -Format "yyyyMMdd-HHmmss")
}

$items = @()
$i = 0
foreach ($row in $rows) {
    $i += 1

    $title = Get-RowValue -Row $row -Candidates @("title", "name", "summary")
    $content = Get-RowValue -Row $row -Candidates @("content", "body", "description", "desc")
    $url = Get-RowValue -Row $row -Candidates @("url", "link", "note_url")
    $author = Get-RowValue -Row $row -Candidates @("author", "user_name", "nickname")
    $platform = Get-RowValue -Row $row -Candidates @("platform")
    $sourceRef = Get-RowValue -Row $row -Candidates @("source_ref", "sourceRef")
    $noteId = Get-RowValue -Row $row -Candidates @("note_id", "noteId", "id")
    $capturedAt = Get-RowValue -Row $row -Candidates @("captured_at", "capturedAt", "created_at")

    $tagsRaw = $null
    $tagsProp = $row.PSObject.Properties["tags"]
    if ($null -ne $tagsProp) { $tagsRaw = $tagsProp.Value }

    $likes = To-IntOrNull (Get-RowValue -Row $row -Candidates @("likes", "like_count"))
    $collects = To-IntOrNull (Get-RowValue -Row $row -Candidates @("collects", "favorite_count"))
    $comments = To-IntOrNull (Get-RowValue -Row $row -Candidates @("comments_count", "comment_count"))
    $shares = To-IntOrNull (Get-RowValue -Row $row -Candidates @("shares", "share_count"))
    $impressions = To-IntOrNull (Get-RowValue -Row $row -Candidates @("impressions", "view_count"))

    if ([string]::IsNullOrWhiteSpace($platform)) { $platform = $DefaultPlatform }
    if ([string]::IsNullOrWhiteSpace($capturedAt)) { $capturedAt = (Get-Date).ToString("o") }
    if ([string]::IsNullOrWhiteSpace($sourceRef)) {
        if (-not [string]::IsNullOrWhiteSpace($noteId)) {
            $sourceRef = "xhs:$noteId"
        }
        elseif (-not [string]::IsNullOrWhiteSpace($url)) {
            $sourceRef = "url:$url"
        }
        else {
            $sourceRef = "row:$i"
        }
    }

    $metrics = @{
        likes = $likes
        collects = $collects
        comments_count = $comments
        shares = $shares
        impressions = $impressions
    }

    $item = @{
        title = $title
        content = $content
        url = $url
        author = $author
        platform = $platform
        metrics = $metrics
        tags = (To-TagsArray $tagsRaw)
        captured_at = $capturedAt
        raw = $row
        source_ref = $sourceRef
    }

    $items += $item
}

$payload = @{
    domain_slug = $DomainSlug
    account_id = ""
    source = $Source
    source_run_id = $SourceRunId
    entity_type = $EntityType
    items = $items
}

$json = $payload | ConvertTo-Json -Depth 100
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($OutputFile, $json, $utf8NoBom)

Write-Host "Built payload file: $OutputFile"
Write-Host "Entity type      : $EntityType"
Write-Host "Rows             : $($items.Count)"
Write-Host "source_run_id    : $SourceRunId"
