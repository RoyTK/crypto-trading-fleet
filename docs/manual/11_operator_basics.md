## Operator Basics — Keeping It Alive

_Last reviewed: 2026-07-02_

This section is for the day-to-day "is it healthy, and what do I do if not" job. You do
**not** need to understand the code.

### What's running, and where

Everything runs on a **rented Linux server from Hetzner** (a company in Germany), inside
"containers" managed by a tool called **Docker**. You normally never touch the server —
it runs by itself. The pieces are: the COPY bot (running its three strategies), a database
(stores all the trades), a dashboard (Grafana), and an alerter (sends Discord messages). The
wallet-discovery work is the one task done from **Roy's office Windows PC**, not the server.

### How to tell it's healthy (your 2-minute daily check)

1. **Discord #alerts channel** — this is the heartbeat. If it's quiet or only shows routine
   green/blue messages, things are fine. Red/urgent messages need attention (see *Alerts*
   below).
2. **The daily digest** — once a day a summary of COPY's last 24 hours is posted to Discord
   **and emailed to `trading@generalaisystems.com`**. Skim it (check that inbox if you
   don't watch Discord closely).
3. **Grafana dashboard** (a web page; login on the Access Sheet) — shows charts of each
   bot's health. You don't need to interpret every chart; just confirm it's loading and
   nothing is screaming red.

If all three look normal, you're done. That's the whole daily job most days.

### The alert levels — and what to do

Alerts come with a severity. **The level tells you how fast to react:**

| Level | How it reaches you | What it means | What to do |
|---|---|---|---|
| **P0** | **Discord ping** + Telegram | Emergency — trading halted, an emergency stop fired, or a big loss limit hit | **Look now.** Open Discord, read the message. If unsure, it's safe to leave the bots halted and call the technical contact. |
| **P1** | Discord ping (mention) | A bot was stopped, the system went quiet, or a "is this strategy failing?" warning fired | **Look within an hour.** Read the message. A "kill-criteria" warning is informational — it does **not** require you to do anything except be aware (a human decides later). |
| **P2** | Discord message (no ping) | Routine but worth noting — wallets added, a credit snapshot, a minor anomaly | Read when convenient. |
| **P3** | Folded into the daily digest | Pure information | No action. |

> **Note:** **SMS text alerts are NOT set up** (the Twilio service wasn't worth paying for
> during the paper phase). So even a P0 emergency reaches you via **Discord + Telegram**,
> not a text message. Keep Discord notifications on. If real-money trading is ever turned
> on, adding SMS is worth reconsidering.

When in doubt about any alert: **a stopped bot is safe**. Nothing bad happens by halting.

### The emergency stop: `/panic`

In Discord, typing **`/panic`** immediately halts every bot, cancels open orders, and
closes positions. Use it any time something feels wrong and you can't reach Roy. It is
designed to be used. (Only authorized Discord users can run it — make sure at least one
person besides Roy is set up; see the Engineer track if not.)

### What NOT to touch

These are the few ways a non-expert could actually cause harm. Avoid them unless an
engineer is guiding you:

- **Do not flip the "live money" switches.** There are settings named like
  `COPY_LIVE_ENABLED` / `COPY_LIVE_FULL_ENABLED` / `COPY_SOLANA_PRIVATE_KEY`. They are
  **off/empty** on purpose. Turning them on makes the bot trade **real money**.
- **Do not change the bots' strategy settings** (the `config.py` files) during the
  measurement window. It corrupts the experiment.
- **Do not delete the database** or the server.
- **Do not commit passwords** anywhere (more in the Access Sheet section).

When unsure, the safe move is always: **halt with `/panic`, then ask.**

### The rhythm of the job

- **Daily:** the 2-minute health check above.
- **Every few days:** run a **wallet discovery pass** to keep COPY's wallet list fresh
  (see *Recurring Tasks*). This is the main recurring chore.
- **Occasionally:** check Helius credit usage so the monthly bill stays in budget (see
  *Recurring Tasks*).
- **On a red alert:** follow the table above.
