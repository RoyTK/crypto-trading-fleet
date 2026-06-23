# register_task.ps1  — run ONCE in an elevated PowerShell to create the schedule.
# Single task, ONE trigger repeating every 13 HOURS (drifting). 13 is coprime to 24,
# so the run time advances through every hour of the day over ~13 days — catching
# traders active in every timezone, not just a fixed US-evening window. First run 09:00.

$TaskName = 'CryptoWalletDiscovery'
$Script   = 'C:\Projects\CryptoTradingworkflow\bots\copy\discovery_automation\run_wallet_discovery.ps1'
$User     = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""

# Every 13 hours, drifting. RepetitionDuration is a long finite span (10y) — this avoids
# the [TimeSpan]::MaxValue quirk while being effectively forever.
$start   = (Get-Date).Date.AddHours(9)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 13) `
    -RepetitionDuration  (New-TimeSpan -Days 3650)

# LogonType Interactive = runs only while you're logged on and the session is unlocked.
# Required for the Chrome browser path, and lets Max-plan OAuth creds be read from the
# user keychain. (If you go full API/headless with a stored key, switch to -LogonType S4U.)
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings -Force

Write-Host "Registered '$TaskName' — first run $start, then every 13 hours (drifting)."

# Verify the repetition actually stuck — Task Scheduler can silently drop -RepetitionInterval.
$rep = (Get-ScheduledTask -TaskName $TaskName).Triggers.Repetition
if ($rep.Interval -eq 'PT13H') {
    Write-Host "Repetition confirmed: Interval=$($rep.Interval)."
} else {
    Write-Warning "Repetition did NOT stick (Interval='$($rep.Interval)'). Re-run, or apply the .Repetition workaround."
}
Write-Host "Test now with:  Start-ScheduledTask -TaskName $TaskName"
