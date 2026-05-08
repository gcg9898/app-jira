# publish-release.ps1 — Creates/updates a GitHub Release with JiraBoard.exe
# Requires GITHUB_TOKEN env var or .env file in the same directory
# Usage: powershell -ExecutionPolicy Bypass -File publish-release.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExePath = Join-Path $ScriptDir "dist\JiraBoard.exe"
$EnvPath = Join-Path $ScriptDir ".env"
$Repo = "gcg9898/app-jira"
$TagName = "latest"

# Load token from .env if not in environment
if (-not $env:GITHUB_TOKEN) {
    if (Test-Path $EnvPath) {
        foreach ($line in Get-Content $EnvPath -Encoding UTF8) {
            if ($line -match '^\s*GITHUB_TOKEN\s*=\s*(.+)$') {
                $env:GITHUB_TOKEN = $Matches[1].Trim()
            }
        }
    }
}

if (-not $env:GITHUB_TOKEN) {
    Write-Host ""
    Write-Host "  ERROR: GITHUB_TOKEN not found." -ForegroundColor Red
    Write-Host "  Add GITHUB_TOKEN=ghp_... to your .env file" -ForegroundColor Yellow
    Write-Host "  Create one at: https://github.com/settings/tokens" -ForegroundColor Yellow
    Write-Host "  Required scope: repo (or just public_repo for public repos)" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

if (-not (Test-Path $ExePath)) {
    Write-Host "  ERROR: $ExePath not found." -ForegroundColor Red
    exit 1
}

$Headers = @{
    "Authorization" = "Bearer $($env:GITHUB_TOKEN)"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "JiraBoard-Publisher"
}
$ApiBase = "https://api.github.com/repos/$Repo"

# Get short SHA for release name
$ShortSha = (git -C $ScriptDir rev-parse --short HEAD 2>$null)
if (-not $ShortSha) { $ShortSha = "unknown" }

Write-Host ""
Write-Host "  Publishing release ($ShortSha)..." -ForegroundColor Cyan

# Check if 'latest' release exists
$release = $null
try {
    $release = Invoke-RestMethod -Uri "$ApiBase/releases/tags/$TagName" -Headers $Headers -Method Get
} catch {
    # 404 = doesn't exist yet
}

# Delete old release if it exists (to replace the asset)
if ($release) {
    Write-Host "  Deleting old release..." -ForegroundColor DarkGray
    try {
        Invoke-RestMethod -Uri "$ApiBase/releases/$($release.id)" -Headers $Headers -Method Delete | Out-Null
    } catch {}
    # Also delete the tag
    try {
        Invoke-RestMethod -Uri "$ApiBase/git/refs/tags/$TagName" -Headers $Headers -Method Delete | Out-Null
    } catch {}
    Start-Sleep -Seconds 1
}

# Create new release
Write-Host "  Creating release..." -ForegroundColor DarkGray
$body = @{
    tag_name   = $TagName
    name       = "JiraBoard $ShortSha"
    body       = "Auto-generated release (commit $ShortSha)"
    draft      = $false
    prerelease = $false
} | ConvertTo-Json

$newRelease = Invoke-RestMethod -Uri "$ApiBase/releases" -Headers $Headers -Method Post `
    -ContentType "application/json" -Body $body

$uploadUrl = $newRelease.upload_url -replace '\{.*\}', ''

# Upload .exe
Write-Host "  Uploading JiraBoard.exe..." -ForegroundColor DarkGray
$fileBytes = [System.IO.File]::ReadAllBytes($ExePath)
$uploadHeaders = @{
    "Authorization" = "Bearer $($env:GITHUB_TOKEN)"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "JiraBoard-Publisher"
    "Content-Type"  = "application/octet-stream"
}

Invoke-RestMethod -Uri "${uploadUrl}?name=JiraBoard.exe&label=JiraBoard.exe" `
    -Headers $uploadHeaders -Method Post -Body $fileBytes | Out-Null

Write-Host ""
Write-Host "  Release published! ($ShortSha)" -ForegroundColor Green
Write-Host "  https://github.com/$Repo/releases/tag/$TagName" -ForegroundColor DarkGray
Write-Host ""
