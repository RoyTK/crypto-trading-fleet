## Why It Is The Way It Is

_Last reviewed: 2026-07-02_

The load-bearing decisions behind the design. A maintainer should understand these before
changing anything — several look like "inefficiencies" but are deliberate. Full history is
in `memory/project_decision_log.md` and `memory/project_fleet_design_state.md`.

- **STRUCTURE was decommissioned on 2026-06-25, on purpose.** It was steadily losing paper
  money, and the suspected fix needs a ~$500/month real-time data-feed upgrade not worth
  paying for during the paper phase. It was first paused, then fully removed when its idle
  container kept running as a "zombie" — spamming polling errors and a recurring cross-bot
  cron failure. Removal covered docker-compose, the kill-criteria and drawdown monitors, and
  the scoring crons. STRUCTURE's code is intact (`bots/structure/`) and can be revived later
  if/when the data spend is justified — don't delete it. So the fleet is now COPY-only and
  all effort is concentrated there.

- **Paper-first, with a kill-criteria window.** The entire point is to *measure* whether an
  edge exists before risking money. Hence locked config during the window, and a Sharpe/win-rate
  scorecard. Don't short-circuit it by going live early or tuning mid-window.

- **The bottom line is profitability, not a composite score.** The old "promotion_score"
  was built for the original 4-bot competition (which bot wins the slot). With the fleet now
  COPY-only, that framing is gone — each strategy is judged on **net PnL**, plain and simple.
  The daily digest and Grafana show net PnL per strategy; promotion_score was retired.

- **COPY now runs three strategies, not one.** *Cluster* (co-buy), *conviction*
  (single-wallet deliberate accumulation), and *teamfollow* (an experiment: ≥2 members of a
  known co-buying "team"). They share the exit/paper-fill machinery but keep isolated
  bankrolls, metrics, and halt ids so one can't mask another. Team-follow is explicitly a
  forward test of the "many small losses, rare moonshots pay" thesis — it's allowed to run
  net-negative on a small sample while that's measured.

- **Discovery-then-validate, staged not auto-promoted.** The `scrape_runners` cron mines
  freshly-run tokens for the wallets that accumulated *before* the run, but it only **stages**
  candidates into a research table — nothing it finds trades until it's separately vetted or
  forward-validated. Raw recurrence is a "spray farm"; only a thin validated subset is
  actually followable, so promotion stays deliberate.

- **kill-criteria alerts a human; it does not auto-halt for strategy.** Deliberate — to
  keep human judgment in the loop and avoid bots reacting to bots. (Severe drawdown limits
  *do* auto-halt; that's different.)

- **COPY's edge is "ride the pump, exit before the rug."** This is why a high rug-rate on a
  wallet is *good*, why vetting rewards **realized** PnL and short holds, and why fast
  mechanical exits (tiered ladder, sell-cluster, rug check) exist. Don't "fix" the bot to
  avoid rug-prone tokens — that's the opposite of the strategy.

- **Vetted-only, quality-over-quantity wallet pool.** Auto-discovery is ~90% noise, so
  wallets are vetted before they can trade, and the raw discovery crons are **paused**.
  Vetted KEEP wallets go **straight to active** (vetted wallets should be *used*, not parked
  cold). The active target is **300**, grown deliberately and watched against the Helius
  credit budget.

- **The "too fast / too slow" wallet brackets.** COPY can only follow wallets it can
  realistically execute alongside: ~30 min–7 day holds. Faster snipers (seconds) are
  flagged **TOO_FAST** (logged for a possible future MEV-grade bot, not used). Slow swing
  whales are parked as a separate "SWING-COPY" idea. A wallet being *profitable for itself*
  is not the same as *profitable for COPY to follow* — that distinction drove unpinning the
  ultra-fast wallets.

- **Wallet demotion is by attributed PnL, not raw activity.** The best wallet does ~1,500
  swaps/day; an activity filter would wrongly kill it. So losers are culled by their COPY
  attributed PnL, with "one rug ≠ a loser" protection (judge excluding the single worst
  trade).

- **Helius cost is webhook deliveries (~99.7%).** Cost scales with subscribed wallets ×
  their activity; MM/HFT bots are the expensive ones (and bad signal), so the vetting that
  rejects them is also the budget control. Both active and watch tiers are subscribed.

- **Browser discovery is attended, not headless.** Headless `claude -p` has no
  Claude-in-Chrome tools, so a scheduled fully-automated browser run is impossible with this
  toolchain. The truly-unattended path would be a paid Birdeye API tier (declined on cost).
  So discovery is a person-initiated task.

- **Auto-pull deploy + commit-don't-scp.** `git push` deploys in ~60s; `reset --hard` wipes
  untracked files, so anything the bot needs is committed, not copied onto the server.

- **No secrets in git.** All keys live in the server `.env` (gitignored) and on the offline
  Access Sheet. This is why disaster recovery hinges on the offline copy.
