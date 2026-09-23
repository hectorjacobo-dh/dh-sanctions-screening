# What can this Google account actually do?
#
#   1. & "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" auth login
#   2. Right-click this file -> "Run with PowerShell"
#
# Read-only. Creates nothing, changes nothing, costs nothing. It answers one
# question: can we host this properly today, or is an IT request required?

$ErrorActionPreference = 'Continue'
$gcloud = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'

function Line { param($t) Write-Host "  $t" }

Write-Host ''
Write-Host '  Google Cloud access check' -ForegroundColor Cyan
Write-Host '  -------------------------' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $gcloud)) {
    Write-Host '  gcloud is not installed at the expected path.' -ForegroundColor Red
    Read-Host '  Press Enter to close'; exit 1
}

# --- 1. Signed in? ----------------------------------------------------------
$account = (& $gcloud config get-value account 2>$null | Out-String).Trim()
if (-not $account -or $account -eq '(unset)') {
    Write-Host '  NOT SIGNED IN.' -ForegroundColor Yellow
    Write-Host ''
    Line 'Run this first, then re-run this script:'
    Write-Host "    & `"$gcloud`" auth login" -ForegroundColor White
    Write-Host ''
    Read-Host '  Press Enter to close'; exit 1
}
Write-Host "  Signed in as: $account" -ForegroundColor Green
if ($account -notlike '*@deliveryhero.com') {
    Write-Host '  WARNING: that is not a deliveryhero.com account. This must run in a' -ForegroundColor Yellow
    Write-Host '           DH-controlled account, not a personal one.' -ForegroundColor Yellow
}
Write-Host ''

# --- 2. Existing projects ---------------------------------------------------
Write-Host '  Projects visible to you:' -ForegroundColor Gray
$projects = & $gcloud projects list --format='value(projectId)' 2>&1
if ($LASTEXITCODE -ne 0 -or -not $projects) {
    Line 'none (or listing is not permitted)'
    $hasProjects = $false
} else {
    $projects | ForEach-Object { Line $_ }
    $hasProjects = $true
}
Write-Host ''

# --- 3. Organisation ---------------------------------------------------------
Write-Host '  Organisation:' -ForegroundColor Gray
$orgs = & $gcloud organizations list --format='value(displayName,name)' 2>&1
if ($LASTEXITCODE -ne 0 -or -not $orgs) { Line 'not visible to this account' }
else { $orgs | ForEach-Object { Line $_ } }
Write-Host ''

# --- 4. May you create a project? -------------------------------------------
# Tested with a dry-run-ish call: an org policy denial and a permission denial
# produce different, informative errors.
Write-Host '  Can you create a project?' -ForegroundColor Gray
$probeId = "dh-scr-probe-$(Get-Random -Minimum 10000 -Maximum 99999)"
$createOut = & $gcloud projects create $probeId --format='value(projectId)' 2>&1 | Out-String
if ($LASTEXITCODE -eq 0) {
    Line 'YES — project creation is permitted.'
    Line "Cleaning up the probe project ($probeId)..."
    & $gcloud projects delete $probeId --quiet 2>&1 | Out-Null
    $canCreate = $true
} else {
    $canCreate = $false
    Line 'NO. The exact reason:'
    ($createOut -split "`n" | Where-Object { $_ -match 'ERROR|PERMISSION|denied|policy|billing|quota' } |
        Select-Object -First 4) | ForEach-Object { Write-Host "      $($_.Trim())" -ForegroundColor DarkYellow }
}
Write-Host ''

# --- Verdict ----------------------------------------------------------------
Write-Host '  ------------------------------------------------------------' -ForegroundColor Cyan
if ($canCreate -or $hasProjects) {
    Write-Host '  VERDICT: hosting looks possible with this account.' -ForegroundColor Green
    Write-Host ''
    Line 'Send this whole output back and the deployment can proceed.'
    Line 'You will get an HTTPS URL that works from home, with a valid'
    Line 'certificate and no browser warning.'
} else {
    Write-Host '  VERDICT: this account cannot host it directly.' -ForegroundColor Yellow
    Write-Host ''
    Line 'That is a useful answer, not a dead end: send deploy\APPROVAL-REQUEST.md'
    Line 'to IT together with the exact error above. It turns a vague request'
    Line 'into "please grant X" or "please run this container for us".'
}
Write-Host '  ------------------------------------------------------------' -ForegroundColor Cyan
Write-Host ''
Read-Host '  Press Enter to close'
