## Glossary (plain language)

_Last reviewed: 2026-07-02_

Terms you'll meet in this manual, the dashboards, and Discord — in everyday language.

- **Bot** — a program that trades automatically by following fixed rules. We now have one:
  **COPY** (running three strategies). STRUCTURE was decommissioned 2026-06-25.
- **Paper trading** — pretend trading. The bot records what it *would* have done, with no
  real money. This is the current mode.
- **Shadow trading** — a small fraction (~10%) of paper trades are also done for real with
  tiny amounts, just to check the simulation is realistic. Still not meaningful money.
- **Live trading** — real money. Currently **off**, gated behind explicit switches.
- **STRUCTURE** — a now-**decommissioned** bot that traded Hyperliquid perpetual futures
  (code retained, not running).
- **COPY** — the bot trading Solana memecoins by copying skilled wallets. It runs three
  independent strategies: **cluster**, **conviction**, and **team-follow** (below).
- **Wallet** — a crypto account, identified by a long string of letters/numbers (an
  "address"). COPY watches a curated list of *skilled* wallets.
- **Cluster** — a COPY strategy/signal: when **several tracked wallets buy the same new
  token within ~15 minutes**, that "cluster" of buys is the cue for COPY to buy too.
- **Conviction** — a COPY strategy: buy when a **single** trusted wallet *deliberately
  accumulates* one token (≥3 buys over ≥5 minutes, not a one-off snipe). Own $10k paper
  bankroll and separate metrics.
- **Team-Follow** — an experimental COPY strategy (2026-07-01): buy when **≥2 members of the
  same known "team"** of wallets co-buy the same token, from a 129-team / 338-wallet roster.
  Own $25k paper bankroll; a forward test of the "rare moonshots pay for many small losses"
  idea, net-negative so far on a small sample.
- **Vetting** — checking whether a candidate wallet is actually good to follow. Each gets a
  verdict:
  - **KEEP** — good; the bot should follow it (becomes "active").
  - **REJECT** — bad (a bag-holder, a bot, junk); discard.
  - **TOO_FAST** — genuinely skilled but trades *so* fast (seconds) that COPY can't keep
    up; logged for a possible future faster bot, not used now.
- **Active / Watch / Pruned / Team-Follow** — the "tiers" a wallet can be in:
  - **Active** — followed; its buys can trigger cluster/conviction trades.
  - **Watch** — observed but not driving trades (mostly empty now; vetted wallets go
    straight to active).
  - **Pruned** — dropped; no longer watched.
  - **Team-Follow** — wallets on the team-follow experiment roster (their co-buys feed that
    strategy via a separate Helius webhook).
- **Kill criteria** — the pass/fail scorecard for each strategy over the measurement window
  (win rate, profit, risk-adjusted "Sharpe" number). If a strategy trips a criterion, the
  system **warns a human**; it does not auto-shut-down for this.
- **Net PnL / profitability** — the bottom-line measure now: net profit-or-loss per
  strategy, shown on the daily digest and Grafana. It replaced the old composite "promotion
  score" (which was built for the original 4-bot competition and has been retired).
- **Sharpe** — a number measuring profit *relative to* how bumpy the ride was. Higher is
  better; one of the kill-criteria metrics.
- **Webhook** — a way for the Helius service to instantly notify our system whenever a
  watched wallet trades. Each notification ("delivery") costs a small credit.
- **Credits** — the unit Helius bills in. Our budget is 30 million/month (~$149). Watching
  more/active wallets uses more credits.
- **Rug / rug-pull** — when a memecoin's creators pull out all the money and the token
  collapses to zero. Common; COPY's whole edge is selling *before* this happens.
- **Hetzner** — the company we rent the always-on server from (located in Germany).
- **Docker / container** — the technology that runs each part of the system in its own
  tidy box on the server.
- **Grafana** — the dashboard web page showing health charts.
- **Discord / Telegram** — chat apps the system posts alerts to. `/panic` is typed here.
- **`/panic`** — the emergency stop command (halts all bots).
- **Git / push / deploy** — git is where the code lives; "pushing" a change automatically
  updates ("deploys") the server within about a minute.
- **`.env`** — a private settings file on the server holding all the passwords/keys. Never
  shared or committed to git.
