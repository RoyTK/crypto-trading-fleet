<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that auto-reconciles the COPY
  vetting ledger daily and pushes changes.

.DESCRIPTION
  Creates task "CryptoFleet-ReconcileVetted":
    - Action  : powershell.exe -File scripts\reconcile_vetted_and_push.ps1
    - Trigger : daily at 09:00 local, StartWhenAvailable (catch-up if PC was off)
    - Runs    : only when the current user is logged on (so it inherits the SSH
                agent + PATH needed for git push). No stored password required.
  Re-running overwrites the existing task (-Force). No admin elevation needed for
  a per-user logged-on task.

  Change the time by editing $At below and re-running.
#>
$ErrorActionPreference = 'Stop'

$TaskName = 'CryptoFleet-ReconcileVetted'
$At       = '9:00AM'
$wrapper  = Join-Path $PSScriptRoot 'reconcile_vetted_and_push.ps1'
if (-not (Test-Path $wrapper)) { throw "wrapper not found: $wrapper" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $wrapper)

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
  -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
  -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
  -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force `
  -Description 'Daily: reconcile bots/copy/vetted_watch_results.txt with the OneDrive copy and push changes so the COPY bot ingests new vetting verdicts.' | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$info = $t | Get-ScheduledTaskInfo
Write-Output ("Registered task '{0}'  (state: {1})" -f $TaskName, $t.State)
Write-Output ("  Action : powershell -File {0}" -f $wrapper)
Write-Output ("  Trigger: daily {0}, StartWhenAvailable (catch-up)" -f $At)
Write-Output ("  Runs as: {0} (only when logged on)" -f $t.Principal.UserId)
Write-Output ("  Next run: {0}" -f $info.NextRunTime)
