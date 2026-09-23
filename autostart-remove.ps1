# Stop the screening engine starting automatically, and stop it now.
#
#   Right-click -> "Run with PowerShell"
#
# Removes only the scheduled task. Your data, cases, evidence and audit trail
# are untouched — they live in the data folder and are not affected.

$ErrorActionPreference = 'Stop'
$taskName = 'DH Sanctions Screening Engine'

Write-Host ''
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $taskName } catch { }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host '  Autostart removed.' -ForegroundColor Green
} else {
    Write-Host '  No autostart task was registered.' -ForegroundColor Yellow
}

# The task may have handed off to a running process; end it too.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*serve.py*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  Stopped process $($_.ProcessId)." -ForegroundColor Gray
    }

Write-Host ''
Write-Host '  Your data is untouched (data\ folder).' -ForegroundColor Gray
Write-Host '  To start it manually again: start.ps1' -ForegroundColor Gray
Write-Host '  To re-enable autostart:     autostart-install.ps1' -ForegroundColor Gray
Write-Host ''
Read-Host '  Press Enter to close'
