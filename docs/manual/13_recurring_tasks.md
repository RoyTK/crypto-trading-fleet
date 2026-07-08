## Recurring Tasks — Step by Step

_Last reviewed: 2026-07-02_

These are the hands-on chores. Each is written as a recipe. They assume you can open a
terminal and Discord; the engineer sections explain the commands in more depth.

### A. Run a wallet discovery + vetting pass (the main chore)

**Why:** COPY only trades on a curated list of skilled wallets. Wallets go stale over time,
so we periodically find new good ones. This is done **attended** (you start it and let it
run) from Roy's office PC, because it drives a web browser.

**Important reality:** this is **not** a hands-off scheduled job. An attempt to run it fully
automatically (Windows Task Scheduler + headless Claude) does **not** work, because the
browser tool only exists in an *interactive* Claude session. So you run it yourself.

**Steps:**
1. On Roy's office PC, make sure **Chrome is open and logged in to Birdeye**
   (birdeye.so). It usually already is.
2. Open **Claude** (the VS Code extension, or an interactive `claude` session with the
   Chrome connection enabled via `/chrome`).
3. Give it the discovery prompt:
   `C:\Projects\CryptoTradingworkflow\bots\copy\discovery_automation\cluster_discovery_vetting_prompt.txt`
   (open that file and paste its contents, or point Claude at it). For the conviction
   strategy's separate roster, use `conviction_discovery_vetting_prompt.txt` in the same
   folder.
4. Let it run. It will browse Birdeye's trending tokens, look at the top traders, and
   **vet each wallet** — marking each KEEP / REJECT / TOO_FAST. It works in batches of ~20
   and saves as it goes, so it can be stopped and restarted without losing work.
5. Results are written to the OneDrive file:
   `C:\Users\Roy\OneDrive\Documents\Claude\vetted_watch_results.txt`.

**Then sync the results into the system — see task B.**

### B. Sync vetting results into the live system

The discovery pass writes to OneDrive. The live bot reads from the project's git repo. So
new results must be copied over and "applied."

The simplest reliable way (the way Roy and Claude have been doing it):
1. In a Claude session **in the project** (`C:\Projects\CryptoTradingworkflow`), ask it to
   "sync the OneDrive vetting file into the repo." It will compare
   `C:\Users\Roy\OneDrive\Documents\Claude\vetted_watch_results.txt` against the repo's
   `bots/copy/vetted_watch_results.txt`, append only the **new** rows (de-duplicated), and
   commit + push.
2. Pushing to git auto-deploys to the server within ~1 minute.
3. The server's daily job then **applies** the verdicts: KEEP wallets become **active**
   (the bot starts following them), REJECT wallets are removed, TOO_FAST are logged only.
   - To apply immediately instead of waiting for the daily job, an engineer runs on the
     server: `docker compose exec -T framework python -m scripts.apply_vetting_results --file /tmp/vw.txt`
     (after copying the file in — see the Engineer track).

You'll see a Discord summary when wallets are applied.

### C. Check Helius credit usage (watch the bill)

**Why:** the COPY bot uses a paid service called **Helius** to receive wallet activity.
Cost is driven almost entirely by how many wallets it watches and how active they are. The
budget cap is **30 million credits/month (~$149)**. You want to make sure adding wallets
isn't blowing the budget.

> **Helius sends its usage alerts and bills to `roytkelly@gmail.com`.** Watch that inbox
> for "approaching credit limit" / billing emails — that's the early warning before the
> monthly cap, separate from the in-system snapshot below.

**Two ways:**

**Quick (no login) — how many credits we're burning, from our own data:** an engineer runs
on the server: `docker compose exec -T framework python -m scripts.credit_pool_snapshot`.
It posts a Discord summary (wallets, daily "deliveries" ≈ daily credit burn, and a
projection vs the 30M cap). This also runs automatically each day.

**Full (the dashboard) — the actual bill:** the Helius dashboard must be opened **through
the server** (the account was created from the server's German internet connection). The
method is an **SSH SOCKS tunnel**:
1. On your PC, open a terminal and run (keep it open while browsing):
   `ssh -D 9999 -N fleet hetzner.com`
   *(Use this exact form — `fleet` then a space then `hetzner.com`, port 9999. The
   "obvious" `fleet@hetzner.com` version does not work.)*
2. In **Firefox**, set a SOCKS proxy: Settings → search "proxy" → Manual proxy → SOCKS Host
   `127.0.0.1`, Port `9999`, choose **SOCKS v5**, check **"Proxy DNS when using SOCKS v5"**.
3. Visit `https://ifconfig.me` to confirm you're now coming from the German server IP, then
   open `https://dashboard.helius.dev` → log in (Access Sheet) → **Usage** shows credits
   used vs the cap.
4. When done, **turn the Firefox proxy back off** so normal browsing works.

If credits are trending toward the 30M cap, slow down how often you run discovery (task A),
or talk to the engineer about raising the budget.

### D. Responding to alerts

See the alert table in *Operator Basics*. In short:
- **P0 (text message):** look immediately; if unsure, leave bots halted and call the
  technical contact.
- **P1 (Discord ping):** look within the hour; a "kill-criteria" warning is just
  information.
- **P2/P3:** read when convenient.

### E. Runner-scraper (pre-run accumulator discovery) — automatic, no action

Once a day (04:10 UTC) the server automatically runs `scripts/scrape_runners.py`. It finds
tokens that just "ran" (pumped), figures out when each run started, and records the wallets
that were **quietly accumulating before the run** into a research table — then prints a
report of wallets that keep showing up across multiple runners. **You don't run anything**
and it does **not** add anyone to a live list; it only *stages* candidate wallets for later
vetting/forward-testing (see task A). It's a research feed, not a trading change. If you
want to see its output, an engineer can read `~/logs/scrape_runners.log` on the server.
Promising wallets are now tracked over time in a **`recurrence_candidates` ledger** (the cron
upserts it each run, preserving any manual `validated`/`rejected`/`watching`/`promoted` status),
so genuine low-frequency accumulators are flagged the moment they clear the bot filter and can be
forward-validated with `scripts/fwd_validate_candidates.py` before any roster promotion.

### F. Slow-cluster shadow detector — automatic, no action (research/forward test)

Once a day (05:30 UTC) the server runs `scripts/slow_cluster_detector.py`. It's a **shadow signal
that never trades**: for each recent traction token (aged 3–6 days) it records how many *genuine*
(bot-filtered) wallets accumulated ≥$200 over the token's first ~3 days **and held**, then tracks
each token's forward price multiple in the `slow_cluster_signals` table (a `is_signal` group with
≥3 such holders, plus a `<3` control group). It's the forward-causal test of whether slow multi-
wallet accumulation predicts a run — decide from the signal-vs-control comparison after ~3–4 weeks.
Log: `~/logs/slow_cluster_detector.log`.
