# Make the screening engine start by itself at every Windows login.
#
#   Right-click this file -> "Run with PowerShell"
#
# Registers a per-user scheduled task. No administrator rights are needed, and
# nothing outside your own user account is touched. To undo it, run
# autostart-remove.ps1.

$ErrorActionPreference = 'Stop'

$root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend  = Join-Path $root 'backend'
$pythonw  = Join-Path $backend '.venv\Scripts\pythonw.exe'
$serve    = Join-Path $backend 'serve.py'
$taskName = 'DH Sanctions Screening Engine'

Write-Host ''
Write-Host '  DH Sanctions Screening Engine - autostart setup' -ForegroundColor Cyan
Write-Host '  ----------------------------------------------' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $pythonw)) {
    Write-Host "  ERROR: $pythonw not found." -ForegroundColor Red
    Write-Host '  Run start.ps1 once first to create the environment, then re-run this.' -ForegroundColor Red
    Read-Host '  Press Enter to close'
    exit 1
}
if (-not (Test-Path $serve)) {
    Write-Host "  ERROR: $serve not found." -ForegroundColor Red
    Read-Host '  Press Enter to close'
    exit 1
}

# pythonw.exe has no console window, so the app runs invisibly in the
# background. Logs go to data\server.log instead.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$serve`"" -WorkingDirectory $backend

# Two triggers, and the second one is the important one.
#
#   1. At logon (delayed 30s — at logon the network stack is often not ready,
#      and the first thing this app does is fetch five regulator feeds).
#
#   2. Every 5 minutes, forever. This is the watchdog. Task Scheduler's
#      "restart on failure" only covers a task that fails to *launch* — a
#      running process that is killed, crashes, or is closed leaves nothing to
#      restart it until the next logon. Verified by killing it and waiting two
#      minutes: it stayed down.
#
#      The 5-minute run is free when the app is already healthy: serve.py
#      probes /api/health first and exits immediately if something is already
#      answering. So this costs nothing when up, and recovers within 5 minutes
#      when down.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = 'PT30S'

$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
# "Indefinitely" — otherwise repetition stops after the default duration.
$watchdog.Repetition.Duration = ''

$triggers = @($logon, $watchdog)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host '  Replacing the existing task...' -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Description 'Runs the DH Group sanctions screening engine on http://127.0.0.1:8000 at login.' | Out-Null

Write-Host '  Registered.' -ForegroundColor Green
Write-Host ''
Write-Host '  Starting it now so you do not have to log out...' -ForegroundColor Gray
Start-ScheduledTask -TaskName $taskName

$up = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        if ((Invoke-WebRequest 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
            $up = $true; break
        }
    } catch { }
}

Write-Host ''
if ($up) {
    Write-Host '  RUNNING: http://127.0.0.1:8000' -ForegroundColor Green
    Write-Host ''
    Write-Host '  It will now start automatically every time you log in.' -ForegroundColor Green
    Write-Host '  There is no window to keep open - it runs in the background.' -ForegroundColor Gray
    Start-Process 'http://127.0.0.1:8000'
} else {
    Write-Host '  Registered, but it did not answer within 20 seconds.' -ForegroundColor Yellow
    Write-Host "  Check the log: $root\data\server.log" -ForegroundColor Yellow
}

Write-Host ''
Write-Host '  Useful:' -ForegroundColor Gray
Write-Host '    Stop now      : Stop-ScheduledTask -TaskName "DH Sanctions Screening Engine"' -ForegroundColor DarkGray
Write-Host '    Start now     : Start-ScheduledTask -TaskName "DH Sanctions Screening Engine"' -ForegroundColor DarkGray
Write-Host '    Remove        : run autostart-remove.ps1' -ForegroundColor DarkGray
Write-Host '    Log           : data\server.log' -ForegroundColor DarkGray
Write-Host ''
Read-Host '  Press Enter to close'
