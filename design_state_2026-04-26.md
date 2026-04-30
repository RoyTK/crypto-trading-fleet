# Crypto Trading Bot Fleet — Design State Snapshot
**Last updated:** 2026-04-26 (all 7 design items complete)
**Status:** Design phase locked. Plan mode active. Awaiting implementation plan + build approval.

---

## Project Goal

Build a fleet of 4 crypto trading bots, paper-trade them in parallel for 8 weeks (per-bot stopwatch), allocate $1k–$20k of real capital to whichever bot(s) demonstrate edge.

- **Primary goal:** Long-term wealth building. Trading capital base should grow over time.
- **Secondary goal:** Income supplement (gated until wealth-building proven).
- **Hurdle rate:** Must beat S&P 500 (~10% nominal) meaningfully. Target 25%+ CAGR.
- **Risk tolerance:** Up to 50% drawdown acceptable in pursuit of asymmetric upside (10–100x).
- **Working directory:** c:\Projects\CryptoTradingworkflow

## Existing Holdings (as of 2026-04-25)

- Coinbase: ~$23,000
- Trust Wallet: ~$2,000
- Ledger Nano X: minimal BTC (cold storage, untouched)

**Planned reallocation at deployment:**
- ~$20k Coinbase → Kraken Pro (better fees, cheaper rails to Hyperliquid/DEXes)
- ~$3k Coinbase buffer (untouched)
- ~$2,520 Service Reserve (initial 9-month seeding from Coinbase, drops to 6-month steady-state via bot profits)
- Smoke-test trading: $1k–$3k from this pool

---

## The 4 Bots (orthogonal edge sources)

### Bot 1 — SNIPER
- **Edge:** Solana memecoin launches (pump.fun, LetsBonk, Clanker)
- **Strategy:** Hybrid 50/50 pre-graduation / post-graduation
- **AI?** No — strictly deterministic rule-based filters
- **Per-trade cap:** 5% of bot capital
- **DD halts:** 8% daily / 20% weekly / 40% total (TIGHT)
- **Allocation cap:** 40% of fleet (variance discount)

### Bot 2 — COPY
- **Edge:** Cluster-following on curated insider/smart-money wallets
- **Strategy:** N wallets buy same token within ±M minutes above $K → trigger. Auto-prune wallets at 60 days inactivity.
- **AI?** No — deterministic cluster math
- **Per-trade cap:** 8% of bot capital
- **DD halts:** 12% daily / 28% weekly / 50% total (MEDIUM)
- **Allocation cap:** 50% of fleet
- **Phase 2 upgrade path:** PRE-WALLET sourcing layer (parked, see below)

### Bot 3 — STRUCTURE
- **Edge:** Hyperliquid market structure
- **Strategy:** Three signals — Funding Fade (2x lev), Liquidation Cascade (3x lev), Whale Flip (2x lev)
- **AI?** No — deterministic threshold-based
- **Per-trade cap:** 15% notional (before leverage)
- **DD halts:** 15% daily / 30% weekly / 45% total (looser daily/weekly, tighter total)
- **Allocation cap:** 60% solo / 50% multi-bot

### Bot 4 — EVENT
- **Edge:** CEX listing front-running + breaking news from Twitter
- **Strategy:** GitHub repo polling, Binance/Coinbase/Upbit listing scrapers, Twitter streaming
- **AI?** YES — local Ollama model on Hetzner for tweet semantic classification (Llama 3.2 3B or Qwen 2.5 3B). $0/mo. ~1-2 days extra build.
- **Per-trade cap:** 10% of bot capital
- **DD halts:** 12% daily / 28% weekly / 50% total (MEDIUM)
- **Allocation cap:** 50% of fleet

### Bot 5 — PRE-WALLET (PARKED)
- Pump-and-dump operator pre-wallet tracking via reverse inference from past pumps
- Real edge but 80% infra overlap with COPY → defer as Phase 2 upgrade to COPY rather than standalone
- Manual research using Bubblemaps free tier (~2 hr/week) during weeks 1-8 as cheap groundwork
- Re-evaluate post week-8 results

---

## 7-Item Design Agenda — Status

| # | Item | Status |
|---|------|--------|
| 1 | Paper-trading mechanics (sim + calibration) | ✅ DONE |
| 2 | Competition scoring & promotion criteria | ✅ DONE |
| 3 | Kill-switches and risk controls | ✅ DONE |
| 4 | Capital allocation logic for live phase | ✅ DONE |
| 5 | Build order & weekly sequencing | ✅ DONE |
| 6 | Data & monitoring stack details | ✅ DONE |
| 7 | Specific signal thresholds per bot | ✅ DONE |

---

## Item #1 — Paper Trading Mechanics ✅

**Fill simulator: Median-realistic per bot.**
- SNIPER: simulates Jito tip competition from real bundle data. 5-15% adverse entry slippage, 10-30% exit slippage in first 60s.
- COPY: 1-block delay fills (Solana ~400ms, EVM 12s). Pool-impact slippage 1-3% liquid, 5-15% freshly-pumped.
- STRUCTURE: replays Hyperliquid orderbook. Computed depth-weighted slippage.
- EVENT: DEX bot competition for pre-listing buys; CEX volatility/queue for post-listing.

**Calibration: Shadow live execution.** ~10% of paper signals fire $5-20 real orders. Compare actual vs simulated fill weekly. ~$50-100/mo additional cost during paper phase.

**Failed-trade tracking:** simulator must log when a fill *would have failed* (no-fill events count against the bot's record).

---

## Item #2 — Competition Scoring ✅

**PromotionScore formula (multiplicative, all components in [0, 1.5]):**
```
PromotionScore = ReturnScore × RiskScore × ConfidenceScore × RegimeScore × CalibrationScore
```

**Components and floors (STRICT — possible no bot promotes at week 8):**
| Component | Formula | Floor |
|-----------|---------|-------|
| ReturnScore | min(1.5, NetReturn% / 30%) | ≥ +5% net return |
| RiskScore | 1.0 - (MaxDD% / 60%) | ≤ 50% max DD |
| ConfidenceScore | min(1.0, EffectiveTradeCount / 50) | ≥ 15 trades |
| RegimeScore | regimes profitable / regimes occurred | ≥ 2 regimes |
| CalibrationScore | 1.0 - \|1.0 - calibration_ratio\| | ≥ 0.6 ratio |

**EffectiveTradeCount = NumTrades × WinRateConfidence** (anti-gaming — random padding produces near-zero score).

**Promotion thresholds:**
- ≥ 1.0: Strong promote (full per-bot budget)
- 0.5–0.99: Conditional promote (half budget, 2-week watch)
- 0.2–0.49: Extended paper (4 more weeks)
- < 0.2: Kill (lenient threshold)

**Cutoff: Rolling after week 6.** Any bot crossing PromotionScore ≥ 1.0 after day 42 (of its own clock) promotes immediately. Day-56 snapshot for the rest.

**Anti-gaming armor:**
- Bot has no knowledge of its own score formula or thresholds
- Scoring engine is a separate process reading trade logs
- Signal logic frozen during paper window (any change resets that bot's clock)
- No epsilon-greedy random exploration trades allowed
- Trade frequency anomalies (>2× rolling avg) flagged for manual review
- Final scores sealed at week 8 / threshold crossing — formula immutable thereafter

---

## Item #3 — Kill-Switches & Risk Controls ✅

**Layer 1 — Per-trade:** Position size caps (per bot), server-side stop-losses where supported (Hyperliquid yes, Solana DEX = 2-second software polling), slippage abort (>2× tolerance = skip).

**Layer 2 — Per-bot circuit breakers:** Per-bot custom DD halts. Universal: 8 consecutive losing trades = 24h pause + manual review.

**Layer 3 — Fleet-wide:**
- Fleet daily DD halt at -10%
- Fleet total DD kill at -25% → **auto-resume after 48h IF BTC moved <5% in prior 24h, else stay halted until manual restart**
- Cross-bot correlation alarm: 3 days of all 4 bots losing → manual review

**Layer 4 — Operational:**
- Position reconciliation every 5 min (bot vs venue actual; >0.5% drift = halt + alert)
- Heartbeat: 30s ping; 2 min silent = alert; 5 min silent = auto-restart attempt; failure = hard halt + page
- RPC/API health checks per venue with backup-RPC failover

**Layer 5 — Manual /panic command:**
- **Discord + Telegram both** (redundant access, mobile-friendly)
- Cancels all orders → closes all positions market → halts all bots → confirmation summary
- Authentication via Roy's user ID whitelist on both platforms

**Layer 6 — Audit trail:** Full trade log (signal → simulated fill → actual fill → slippage → fees → PnL → exit reason) in PostgreSQL forever. Every halt event Discord-alerted in real time.

---

## Item #4 — Capital Allocation ✅

**Deployment phases (after promotion):**
- **Phase L1 (smoke):** $1k–$3k total scaled to # of promoted bots (1 bot → $1k; 2 → $2k; 3-4 → $3k). Validation, not profit.
- **Phase L2 (scale-up):** $5k–$10k total in 2-3 tranches if calibration matches paper within ±20%.
- **Phase L3 (full):** $15k–$20k only if Phase L2 returns within ±25% of paper expectations.

**Split when multiple bots promote: Score-weighted within 15-50% bands.**
- Floor 15% per promoted bot (no token allocation)
- Cap 50% per bot (60% if solo, 40% for SNIPER variance discount)

**Profit Cascade (3 buckets, fills in priority order):**

**Bucket 1 — Service Reserve (priority #1):**
- **Initial:** 9 months runway (~$2,520) seeded from Coinbase at deployment
- **Steady-state target:** 6 months (~$1,680), refilled by bot profits
- Pays the ~$280/mo infra costs

**Bucket 2 — Fleet Capital Growth (priority #2):**
- After Bucket 1 full, profits flow into fleet treasury
- Monthly review: 50% boosts top-scoring bot's allocation cap, 50% held for diversification
- Effect: trading base grows over time, weighted toward what's working

**Bucket 3 — Personal Withdrawal (priority #3):**
- Unlocks at 50% fleet growth (deployed capital +50% from initial)
- Default 20% of monthly profits available for withdrawal
- **Auto-defer default:** when triggered, system reports availability and Roy makes per-month decision

**Service-cost safety:** If trailing 90d PnL is negative AND Service Reserve drops below 3 months runway → monthly funding decision alert.

**Capital pulled from a losing bot:** Stables until monthly review. No auto-flow to winner.

**Scaling winners:** Trigger >20% net return AND <50% per-trade capacity utilized AND ≥30 days since last add. Increment +25% of current allocation, max once/month, max 3 adds per bot. Bounded by total fleet cap.

---

## Item #5 — Build Order & Sequencing ✅

**Build order (easiest-first):** STRUCTURE → COPY → EVENT → SNIPER

| Order | Bot | Why this slot |
|-------|-----|---------------|
| 1st | STRUCTURE | Cleanest API surface (Hyperliquid SDK), least fragile data sources, shakes out shared infra |
| 2nd | COPY | Adds wallet-monitoring infrastructure on top of working core |
| 3rd | EVENT | Adds Twitter streaming + Ollama LLM stack — significant new infra |
| 4th | SNIPER | Solana memecoin tooling is most fragile; benefits from battle-tested infra |

**Pace:** Balanced ~14 weeks total project length (not aggressive, not slow).

**Per-bot stopwatch model (key reframing):**
- Each bot's 8-week paper clock starts AFTER its own shakedown is clean
- NOT a coordinated week-6 fleet start
- Different bots will pass shakedown at different times — coordinating start dates would either delay strong bots or rush weak ones
- Per-bot clocks let each bot earn its evaluation window honestly

**Shakedown gate:**
- Distinct shakedown gate per bot before its paper clock starts
- Block the next bot's build until prior bot's shakedown is green
- Why: shared infra issues found in bot N's shakedown should be fixed before bot N+1 inherits them

**Build agent staffing (hybrid):**
- One focused agent per bot at a time
- Parallelize only where truly independent (e.g., separate shared-infra tasks during a single bot's build)
- Avoids cross-bot context pollution and lets each agent develop deep familiarity with its bot

**Anti-bias guardrails (Roy self-flagged risk of favoring early-success bots):**
- Documented kill commitment up front, before any bot has results
- Comparative confidence displays — every bot's score visible side-by-side
- Every override of the scoring system gets logged with reasoning
- Fading bots get root-cause-analysis (modify + re-shakedown), not auto-kill
- Only **structural** fade (broken edge) triggers kill; **tactical** fade (tunable) gets a second pass

---

## Item #6 — Data & Monitoring Stack ✅

**Alert taxonomy:**
- **P0 (SMS via Twilio + Discord ping + Telegram):** fleet halt, /panic invoked, position drift > threshold, total DD kill
- **P1 (Discord ping + Telegram):** per-bot DD halt, consecutive loss limit hit, heartbeat silent >2 min
- **P2 (Discord no-ping):** trade entries/exits, calibration drift, per-bot daily DD warning
- **P3 (daily digest only):** routine activity, score updates

**Daily check-in report:**
- 7am local time
- Channel: Discord + email
- Contents: per-bot PnL, scores, trade count, calibration ratio, drawdown state, alerts in last 24h

**Remote access:**
- **Cloudflare Tunnel + auth** during paper phase and early live
- **Trigger to switch to VPN-only at $50,000 deployed capital**
- Why this trigger: Cloudflare Tunnel is fast and adequate while capital is small; VPN-only is the right answer once attack surface = real money

**LLM-assisted ops review:**
- **Separate Anthropic API key** (NOT Claude Max — production observability + ToS reasons)
- Optional weekly trade-log pattern review by Claude
- Cost: a few dollars/month at most, not material

**Stack:**
- PostgreSQL — operational state, trade log, halts, audit trail
- DuckDB — analytics queries, scoring computation
- Redis — cache, pub/sub between bot processes
- Prometheus + Grafana — dashboards (per-bot, fleet-wide, calibration)
- Discord + Telegram bots — alerting + /panic command
- Twilio — P0 SMS only
- Ollama — local LLM for EVENT (separate from any ops-review API)

---

## Item #7 — Signal Thresholds (v0 anchors) ✅

**All v0 numbers below — anchors only, tuned during each bot's shakedown phase based on actual signal-to-noise from real market data.**

### SNIPER

**Rug filter v0 (ALL must pass for pre-grad entry):**
- LP locked ≥ 95%
- Top-10 holders ≤ 25%
- Mint authority revoked + freeze authority revoked
- Bundler concentration ≤ 30%
- ≥ 50 unique buyers in first 60s
- Dev wallet sells = 0 in first 5 min
- Contract verified

**Pre-graduation:**
- 1-3% per snipe
- 15% slippage tolerance
- Dynamic Jito tip
- -40% stop
- Exit ladder: +100% / +300% / +1000%

**Post-graduation:**
- Filter: liquidity ≥ $50k AND volume ≥ $200k in first hour
- 2-5% position
- 8% slippage
- -25% stop

### COPY

**Wallet curation:**
- 6-month PnL ≥ $50k
- Win rate ≥ 55%
- Average hold time 30 min – 7 days
- ≥ 20 trades in last 90 days
- Wallet age ≥ 60 days
- Pool size: 200-300 wallets

**Cluster trigger:**
- 3+ wallets buying same token within ±15 min
- ≥ $5k each
- Token age < 24 hours OR volume jumped > 5× in last hour
- Sizing: 3 wallets = 4% pos, 4-5 wallets = 6%, 6+ wallets = 8%

### STRUCTURE

**Funding Fade:**
- Funding rate > +50% (short signal) or < -30% (long signal)
- OI ≥ $10M, top-20 traded asset
- 5-10% position at 2x leverage
- 5% stop

**Liquidation Cascade:**
- ≥ $5M in liquidations within 5 min AND price moved ≥ 4%
- Top-15 traded asset
- 5-12% position at 3x leverage
- 4% stop, 3% take-profit

**Whale Flip:**
- ≥ $500k notional position flip on curated whale list (30-50 whales, ≥ 60% historical win rate)
- 4-8% position at 2x leverage
- 6% stop

### EVENT

**Detection:**
- Coinbase GitHub repo poll: 30s interval
- Other CEX listing scrapers: 60s interval
- Twitter streaming: 5s lag target
- Watch-list: ~100 accounts (CT influencers, exchange official accounts, key dev accounts)

**Classifier:**
- Ollama prompt template defined (binary: tradeable_event YES/NO with reasoning)
- **0.85 confidence threshold for auto-trade** (conservative — better to miss a few than fire on hallucinations)

**LISTING trade:**
- DEX: 4-8% position, 12% slippage
- CEX: limit order 2% above mark
- -15% stop
- Exit ladder: 50% at +30%, 25% at +100%, trail final 25%

**HACK trade (exchange/protocol exploit):**
- SHORT 6-10% position on Hyperliquid at 1.5x leverage
- 8% stop
- Take 50% profit at -10% on the affected asset

---

## Cost Summary (Lean Tier — Paper Phase)

**Per-bot costs:**
| Bot | Service | Monthly |
|-----|---------|---------|
| SNIPER | Helius Developer + Jito tips (low/$0 in paper) | $50 |
| COPY | Cielo Premium | $40 |
| STRUCTURE | Free SDK + APIs | $0 |
| EVENT | Twitter API v2 Basic | $100 |

**Shared infra:**
| Item | Monthly |
|------|---------|
| Hetzner VPS (CPX31, may bump to CPX41 for Ollama) | $20-30 |
| Hetzner backup | $4 |
| Twilio SMS (P0 only) | ~$5 |
| Discord + Telegram bots | $0 |
| Docker / DuckDB / Postgres / Redis / Prometheus / Grafana / Ollama | $0 (self-hosted) |

**Calibration overhead (paper phase only):**
- Shadow live execution fees | $50-100/mo

**Total paper-phase cost: ~$270–330/mo (call it ~$280/mo budget line)**

**One-time costs:** Ledger Nano X (already owned), KYC time on Kraken/Hyperliquid (free).

**Skipped extras (defer until justified):**
- Nansen Standard ($99/mo) — defer until COPY needs better wallet sourcing
- TradingView Premium ($60/mo) — free tier sufficient
- High Jito tips ($200/mo) — scale per-trade cap, not flat budget
- LLM API for EVENT — replaced by free local Ollama model

---

## AI / LLM Decisions

- **SNIPER, COPY, STRUCTURE:** No AI. Pure deterministic. $0 inference cost.
- **EVENT:** Local Ollama model (Llama 3.2 3B or Qwen 2.5 3B) on Hetzner for tweet semantic classification. $0/mo. ~1-2 days extra build time.
- **Claude Max subscription is NOT used for production bot inference** (ToS, rate limits, no observability).
- **Optional weekly trade-log review** uses a **separate Anthropic API key** (a few dollars/month at most).

---

## Venue Plan

| Venue | Use | Status |
|-------|-----|--------|
| Kraken Pro | Primary spot/fiat hub, ~$20k transfer at deployment | TODO at deployment |
| Coinbase | $3k buffer + Service Reserve, fiat on/off-ramp, backup | Existing |
| Hyperliquid | STRUCTURE perps, EVENT perps | TODO open + fund |
| Solana wallet (Phantom/Solflare) | SNIPER + COPY Solana side | TODO new wallet |
| Base/Arbitrum wallet (Rabby) | COPY EVM side | TODO new wallet |
| Trust Wallet | Existing $2k, untouched | Existing |
| Ledger Nano X | Cold storage, untouched | Existing |

---

## Tech Stack (firmed up via Items 5-6)

- Python 3.11+ with asyncio
- Docker (one container per bot)
- DuckDB (analytics) + PostgreSQL (operational state) + Redis (cache/pub-sub)
- Prometheus + Grafana (monitoring)
- Discord + Telegram webhooks (alerts)
- Twilio (P0 SMS only)
- Ollama (local LLM for EVENT)
- Hetzner Frankfurt VPS (primary), Oracle Free Tier ARM (backup monitoring node)
- Helius (Solana RPC + webhooks)
- Cielo Finance API (wallet tracking for COPY)
- Hyperliquid Python SDK (STRUCTURE)
- Twitter/X API v2 streaming (EVENT)
- Cloudflare Tunnel + auth for remote access (→ VPN-only at $50k deployed)

---

## Roy's Decision Style & Collaboration Patterns

(See [feedback_collaboration.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\feedback_collaboration.md))

- Direct, decisive. Says "Stop" when needed. Wants honest critique, not validation.
- Catches math errors, pushes back on sloppy numbers.
- Prefers structured AskUserQuestion choices over open prompts once direction is set.
- Plan mode discipline: don't build until told.
- Work through agendas one item at a time.
- No emojis, no fluff, terse where possible.

---

## Memory File Index

Persistent memory at `C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\`:

- [MEMORY.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\MEMORY.md) — index
- [user_profile.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\user_profile.md)
- [project_crypto_bot_fleet.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\project_crypto_bot_fleet.md)
- [project_fleet_design_state.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\project_fleet_design_state.md) — all 7 items locked
- [project_decision_log.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\project_decision_log.md) — chronological reasoning
- [feedback_collaboration.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\feedback_collaboration.md)
- [project_pre_wallet_parked.md](C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\project_pre_wallet_parked.md)

---

## What This Doc Is

- A frozen, durable hand-off snapshot — readable cold by a fresh agent or by Roy weeks from now.
- The source of truth for the design decisions that the implementation plan will execute against.

## What This Doc Is NOT

- NOT an implementation plan. The next ExitPlanMode produces that.
- NOT approved for build. ExitPlanMode for build approval comes after the implementation plan is written.
