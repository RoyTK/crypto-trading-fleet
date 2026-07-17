<#
.SYNOPSIS
  Reconcile the COPY vetting ledger (OneDrive <-> repo) and push if it changed.

.DESCRIPTION
  Wrapper invoked by the Windows Scheduled Task "CryptoFleet-ReconcileVetted"
  (daily, catch-up). Steps:
    1. git pull --ff-only  (so the push fast-forwards)
    2. python scripts/reconcile_vetted_results.py --apply
    3. if bots/copy/vetted_watch_results.txt changed -> add + commit + push

  Only the vetted file is staged, so any unrelated work-in-progress in the tree
  is left untouched. Logs to %LOCALAPPDATA%\CryptoFleet\reconcile_vetted.log.

  Register/refresh the task with:
    powershell -ExecutionPolicy Bypass -File scripts\register_reconcile_task.ps1
#>
$ErrorActionPreference = 'Continue'   # native git/python failures are caught via $LASTEXITCODE

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$logDir = Join-Path $env:LOCALAPPDATA 'CryptoFleet'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'reconcile_vetted.log'
$vetted  = 'bots/copy/vetted_watch_results.txt'

function Log($msg) {
  $line = "{0}  {1}" -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')), $msg
  $line | Out-File -FilePath $logFile -Append -Encoding utf8
  Write-Host $line   # host stream only — must NOT pollute function return values
}
function Run($label, [scriptblock]$cmd) {
  $out  = & $cmd 2>&1 | Out-String
  $code = $LASTEXITCODE
  if ($out.Trim()) { foreach ($l in $out.Trim().Split("`n")) { Log ("  | " + $l.TrimEnd()) } }
  return $code
}

Log "=== reconcile run start (repo=$repoRoot) ==="

# 1. Pull so the later push fast-forwards. Abort on a non-clean pull (diverged tree).
if ((Run 'pull' { git pull --ff-only origin main }) -ne 0) {
  Log "git pull --ff-only failed (diverged / network?) -> aborting before any commit"; exit 1
}

# 2. Reconcile + apply. Non-zero = STRUCTURAL corruption (3: broken header / >25% rows bad)
#    or a file missing (2) -> abort. Individually malformed rows no longer block: fused
#    lines are auto-repaired, the rest are quarantined to vetted_watch_results.quarantine.txt
#    (OneDrive dir) and reported in this log while clean rows keep syncing.
$rc = Run 'reconcile' { python scripts/reconcile_vetted_results.py --apply }
if ($rc -ne 0) { Log "reconcile exited $rc -> aborting (no commit)"; exit $rc }

# 2.5 Refresh the cluster co-trader-walk anchors on OneDrive (best-effort: a
#     failure here must NEVER block the vetted reconcile/push). Pulls COPY's
#     active+profitable cluster wallets from the server DB and writes the anchor
#     file browser-Opus reads for discovery route 1.
try {
  $anchorDir  = Join-Path $env:USERPROFILE 'OneDrive\Documents\Claude'
  $anchorFile = Join-Path $anchorDir 'cluster_anchor_wallets.txt'
  $anchors = & ssh claude-fleet "cd ~/crypto-fleet && docker compose exec -T framework python -m scripts.export_cluster_anchors" 2>&1 | Out-String
  if ($LASTEXITCODE -eq 0 -and ($anchors -match 'cluster_anchor_wallets') -and (Test-Path $anchorDir)) {
    [System.IO.File]::WriteAllText($anchorFile, $anchors.Trim() + "`n", (New-Object System.Text.UTF8Encoding($false)))
    $n = ($anchors.Trim().Split("`n") | Where-Object { $_ -notmatch '^\s*#' -and $_.Trim() }).Count
    Log "refreshed cluster anchors ($n wallets) -> $anchorFile"
  } else {
    Log "anchor export skipped (rc=$LASTEXITCODE, dir exists=$(Test-Path $anchorDir)) -> kept previous anchor file"
  }
} catch { Log ("anchor refresh exception (non-fatal): " + $_.Exception.Message) }

# 3. Commit + push only if the tracked vetted file actually changed.
$changed = (& git status --porcelain -- $vetted | Out-String).Trim()
if (-not $changed) { Log "no change to $vetted -> nothing to commit"; Log "=== done ==="; exit 0 }

& git add -- $vetted
$msg = "chore: auto-reconcile vetted_watch_results.txt (OneDrive -> repo)`n`nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
if ((Run 'commit' { git commit -m $msg }) -ne 0) { Log "git commit failed -> aborting"; exit 1 }
if ((Run 'push'   { git push origin main }) -ne 0) { Log "git push failed (auth/network?) -> committed locally, NOT pushed"; exit 1 }

Log "committed + pushed updated vetted ledger"
Log "=== done ==="
exit 0
