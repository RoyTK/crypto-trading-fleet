<#
.SYNOPSIS
  Register/refresh the Windows Scheduled Task "CryptoFleet-BackupDB" — mirrors the
  nightly Hetzner Postgres backup to OneDrive (offsite DR). Daily 04:00, catch-up.
  Mirrors the CryptoFleet-ReconcileVetted registration pattern.
.NOTES
  Run once: powershell -ExecutionPolicy Bypass -File scripts\register_backup_task.ps1
#>
$scriptPath = Join-Path $PSScriptRoot 'mirror_backup_to_onedrive.ps1'
if (-not (Test-Path $scriptPath)) { throw "mirror script not found: $scriptPath" }

$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
             -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -Daily -At 4:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName 'CryptoFleet-BackupDB' `
  -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Nightly mirror of the Hetzner Postgres backup to OneDrive (offsite DR).' `
  -Force | Out-Null

Write-Host "Registered CryptoFleet-BackupDB (daily 04:00)."
Get-ScheduledTask -TaskName 'CryptoFleet-BackupDB' | Select-Object TaskName, State
