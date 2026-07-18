<#
.SYNOPSIS
  Mirror the latest Hetzner Postgres backup to OneDrive (offsite DR copy).
.DESCRIPTION
  Invoked by the Windows Scheduled Task "CryptoFleet-BackupDB" (daily ~04:00, after
  the server-side 03:30 pg_dump). The on-server backup (scripts/backup_db.sh) protects
  against DB corruption; this OneDrive copy protects against losing the VPS itself.
  Uses System32 OpenSSH scp (same host alias as the reconcile task). Keeps 14 days.
  Register/refresh with: powershell -ExecutionPolicy Bypass -File scripts\register_backup_task.ps1
#>
$ErrorActionPreference = 'Continue'

$dest = Join-Path $env:USERPROFILE 'OneDrive\Documents\Claude\Backups\crypto-fleet\daily'
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Force -Path $dest | Out-Null }
$logDir = Join-Path $env:LOCALAPPDATA 'CryptoFleet'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir 'backup_db.log'

function Log($m) {
  $line = "{0}  {1}" -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')), $m
  $line | Out-File -FilePath $log -Append -Encoding utf8
  Write-Host $line
}

Log "=== mirror run start ==="

# Newest server backup filename (full path on the server).
$latest = (& ssh claude-fleet "ls -t ~/backups/fleet_db_*.sql.gz 2>/dev/null | head -1")
if ($latest) { $latest = $latest.Trim() }
if (-not $latest) { Log "no server backup found -> nothing to mirror"; exit 1 }

$name = Split-Path $latest -Leaf
$out = Join-Path $dest $name
if (Test-Path $out) { Log "already mirrored: $name"; exit 0 }

& scp -q "claude-fleet:$latest" "$out"
if ($LASTEXITCODE -ne 0) { Log "scp failed (exit $LASTEXITCODE)"; exit 1 }

$sz = (Get-Item $out).Length
if ($sz -lt 1000000) { Log "mirrored file suspicious ($sz bytes) -> removing"; Remove-Item $out -Force; exit 1 }

# Prune local mirror older than 14 days.
Get-ChildItem $dest -Filter 'fleet_db_*.sql.gz' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force -ErrorAction SilentlyContinue

Log "mirrored $name ($([math]::Round($sz/1MB,1)) MB) -> $dest"
Log "=== done ==="
exit 0
