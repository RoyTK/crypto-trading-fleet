# run_wallet_discovery.ps1
# Runs the Solana wallet discovery/vetting prompt via Claude Code (headless), pinned to Opus.
# Browser drives Birdeye discovery; Claude Code writes qualifying rows to the LOCAL,
# git-tracked results file with its file tools. THIS wrapper then commits + pushes that
# file and posts a row-count health signal to Discord. Windows Task Scheduler 09:00/21:00.
#
# AUTH/BILLING: no --bare (that would force ANTHROPIC_API_KEY = separate API billing).
# Plain `claude -p` uses your logged-in Max creds. We ALSO clear ANTHROPIC_API_KEY from
# this process below, so a project key set elsewhere on the box can't silently divert
# billing to the API.
#
# HEALTH: exit code 0 != success — a dead Chrome extension exits clean with zero rows.
# The real signal is "did new rows get appended this pass", so we diff the file and
# notify on +N / 0 / error.

# ----------------------------- CONFIG -----------------------------
$RepoRoot    = 'C:\Projects\CryptoTradingworkflow'
$ProjectDir  = Join-Path $RepoRoot 'bots\copy'
$PromptFile  = Join-Path $ProjectDir 'browser_discovery_vetting_prompt.txt'
$ResultsFile = Join-Path $ProjectDir 'vetted_watch_results.txt'
$LogDir      = Join-Path $ProjectDir 'logs'
$Model       = 'opus'      # resolves to current Opus (claude-opus-4-8)
$MaxTurns    = 200         # browser scraping ~10 tokens is turn-heavy; 2h task timeout is the real guard
$ClaudeExe   = 'claude'
$GitBranch   = 'main'
# Browser + in-page JS can't be scoped with --allowedTools, so the browser path needs the skip flag.
$PermArgs    = @('--dangerously-skip-permissions')
# Discord webhook for the health signal. Set the COPY_DISCORD_WEBHOOK user env var,
# or paste the URL here. Leave blank to skip Discord (run still logs locally).
$DiscordWebhook = $env:COPY_DISCORD_WEBHOOK
# ------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

# Force Max-plan billing: clear any inherited API key for THIS process only.
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp  = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$outLog = Join-Path $LogDir "run_$stamp.json"
$errLog = Join-Path $LogDir "run_$stamp.err.log"
$lockF  = Join-Path $LogDir 'run.lock'

function Write-Log($msg) {
    "[$stamp] $msg" | Tee-Object -FilePath $errLog -Append | Out-Null
}

function Send-Discord($content) {
    if (-not $DiscordWebhook) { Write-Log 'Discord webhook unset; skipping notify.'; return }
    try {
        $body = @{ content = $content } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $DiscordWebhook -Method Post -ContentType 'application/json' `
            -Body $body -TimeoutSec 20 | Out-Null
    } catch {
        Write-Log "Discord notify failed: $($_.Exception.Message)"
    }
}

function Count-Rows($path) {
    if (-not (Test-Path $path)) { return 0 }
    return (Get-Content -LiteralPath $path | Where-Object { $_.Trim() -ne '' }).Count
}

# Single-instance lock: keep the 21:00 run from colliding with a hung 09:00 run.
if (Test-Path $lockF) {
    $age = (Get-Date) - (Get-Item $lockF).LastWriteTime
    if ($age.TotalHours -lt 3) {
        Write-Log "Previous run still holds lock (age $([int]$age.TotalMinutes)m). Exiting."
        exit 2
    }
    Remove-Item $lockF -Force   # stale lock, clear it
}
New-Item -ItemType File -Path $lockF -Force | Out-Null

$rowsBefore = Count-Rows $ResultsFile
$code = 1
try {
    if (-not (Test-Path $PromptFile)) { throw "Prompt file not found: $PromptFile" }
    $prompt = Get-Content -Raw -Path $PromptFile

    # Pull latest first so dedup reads current state. Non-fatal on failure.
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & git -C $RepoRoot pull --rebase --autostash origin $GitBranch *>> $errLog
    if ($LASTEXITCODE -ne 0) { Write-Log 'git pull --rebase failed (continuing).' }
    $ErrorActionPreference = $eap

    Push-Location $ProjectDir
    $cliArgs = @('-p', $prompt,
                 '--model', $Model,
                 '--max-turns', $MaxTurns,
                 '--output-format', 'json') + $PermArgs

    & $ClaudeExe @cliArgs 1> $outLog 2> $errLog
    $code = $LASTEXITCODE
    Write-Log "claude exited $code. stdout=$outLog"
}
catch {
    Write-Log "WRAPPER ERROR: $($_.Exception.Message)"
    $code = 1
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
    Remove-Item $lockF -Force -ErrorAction SilentlyContinue
}

# ---- Health signal: rows added is the real "did it work", not the exit code ----
$rowsAfter = Count-Rows $ResultsFile
$delta = $rowsAfter - $rowsBefore
Write-Log "rows before=$rowsBefore after=$rowsAfter delta=$delta"

$pushNote = ''
if ($delta -gt 0) {
    # Commit + push ONLY the results file (scoped — never sweep other changes).
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & git -C $RepoRoot add -- $ResultsFile *>> $errLog
    & git -C $RepoRoot commit -m "copy: discovery+vet run $stamp (+$delta rows)" *>> $errLog
    & git -C $RepoRoot push origin $GitBranch *>> $errLog
    if ($LASTEXITCODE -eq 0) { $pushNote = ' pushed' }
    else { $pushNote = ' PUSH FAILED (rows committed locally — resolve manually)' }
    $ErrorActionPreference = $eap
}

if ($code -ne 0) {
    Send-Discord ":x: **COPY discovery/vet FAILED** ($stamp) - claude exit $code, delta=$delta rows. Log: $errLog"
}
elseif ($delta -le 0) {
    Send-Discord ":warning: **COPY discovery/vet: 0 rows** ($stamp) - clean exit but nothing appended. Likely Chrome extension idle or a Cloudflare/login wall. Check the browser connection."
}
else {
    Send-Discord ":white_check_mark: **COPY discovery/vet OK** ($stamp) - +$delta rows.$pushNote"
}

exit $code
