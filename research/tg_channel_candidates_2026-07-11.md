# TG Channel Candidates + Discovery Landscape (2026-07-11)

_Deliverable of the Phase 0 channel-discovery research pass (crypto-bot-trader agent,
web-research only — NO channel was opened live; treat every entry as an UNVETTED
candidate). Companion to `source_inventory_2026-07-11.md`._

**Top-line caveat:** channels confirmed only via search-engine snippets or third-party
indexers (tgstat, telemetr.io, telegramchannels.me). Many names come from templated
affiliate listicles — do NOT trust subscriber counts or "best caller" claims. The study
ranks channels on their own measured hit-rate; reputation is only a seed.

## Tier A — existence confirmed via direct t.me link or independent indexer

| Channel | Handle/link | Notes |
|---|---|---|
| Solana Meme Coins Calls | @SolanaMemeCoinss | tgstat-indexed; self-promo claims — discount |
| Solana100xCall | @solana100xcall | ~18.8K subs (telemetr.io); claims auto sniper-call detection w/ CA |
| Call Analyser (SOL Only) | @CallAnalyserSol | Companion bots @TokenPingBot/@CallAnalyserBot — rare channel-level call-tracking |
| AI Solana Call Bot Free | @solcallbot | Bot-relay/aggregator |
| Pump.Fun Alpha Calls | @pumpfunalphacalls | |
| Pump.fun Chat | @pumpdotfunchat | General chat, not pure-caller |
| Pump Calls | @PUMP_CALLS | |
| Solana Gem Call(s) | @solanagemcall / @SolanaGemCalls | |
| Solana Insiders | @solanainsiders | ~3.9K members — small but real |
| House of Xeus | @HouseOfXeus | ~27.5K subs; named operator @XeusTheGreat on X (cross-platform vetting possible) |
| Alpha Solana | @reversecat_sol | |
| Alpha Calls | @FairLaunchAlpha | |
| SOL Sniper Alpha Calls | @solalphacalls | Premium gated via @ZeroblockXC |
| Crypto Alpha Gems | @CryptoAlphaGems | Mixed-chain |
| Solana Memecoins Alpha Insider Calls | "bestsolanamemecoinscalls" (telegramchannels.me) | |
| Eddy Calls (Solana Insider) | private-invite only | Can't verify without joining |

## Tier B — possibly dead / listicle-only (no independent cross-hit)

MemeCoin Daily (~1.06M claimed — suspicious; likely news aggregator), MemeCoin Whale
Pumps (~151K), Crypto Pumps Island (~78K), FarmercistJournal (~47K), SuperX Memecoin
Copy Trading (~5.6K), Based Kook Calls (~18.8K), DEXTOOLS PUMPS, VIP Solana Pumps.

## Caller-ranking / call-tracking tools

**Structural finding: almost all rank on-chain WALLETS, not TG-posted calls** (wallet
history can't be edited; TG posts can — likely why nobody's built clean call-ranking).

| Tool | URL | Ranks | Notes |
|---|---|---|---|
| Kolscan | kolscan.io/leaderboard | KOL wallets PnL/ROI/WR 24h/7d/30d | Free, no signup — most credible |
| MadeOnSol | madeonsol.com | 1,000+ KOL wallets + coordination detection | Close to our cluster concept |
| GMGN.ai | gmgn.ai | Smart-money wallets + token scanner (insider ratio, bundle-buy, dev-holding%) | Web WAF-blocked headless; TG bot exists |
| Cielo | docs.cielo.finance | Wallet-watch alerts TG/Discord | (We hold a sub until Aug 2) |
| KOLTracker | koltracker.io | Wallet leaderboard | Free |
| XHuntr | xhuntr.io | X-based PRE-on-chain social signals | 0.50 SOL/wk — earlier-in-chain, trial before building X scraping |
| Sect Bot | collectiveshift.io/sect | Member CALL leaderboards | Own $SECT token = conflict-of-interest flag |
| SpyBot / KolSniper | TG bots | Wallet alerts / auto-copy | Unverified marketing claims |

**Gap = our opportunity:** no tool ranks TG callers by post-call performance independent
of their wallets; no "who called this CA first" index exists anywhere. Our Telethon
corpus (CA + timestamp per channel) is the only complete solution.

## Channel-discovery methods

- **tgstat.com** — dominant indexer; paid tier has keyword post-search (~60.8B posts)
  BUT: hard-403s naive scraping, US cards reportedly not accepted, coverage skews
  post-Soviet. Use for manual recon, not automation.
- **Telegago** (Google CSE over t.me) + Google dorks `site:t.me "<CA>"` — free recon.
- **t.me/s/<channel>** — public HTTP preview of recent history, no login; cheap
  liveness spot-check before spending Telethon quota.
- **Lyzem / XTEA / TgramSearch** — minor indexers, free-tier recon.
- Native TG search ≠ full-text cross-channel search (usernames/joined chats mainly).

## Telethon read-only practicalities (2026)

- **No join needed for public-channel history** — resolve once, GetHistory forever.
- GetHistory: 100 msgs/call; **1–2s spacing stable indefinitely**; 5k-post backfill ≈ 50s.
- **Bottleneck = ResolveUsername** — resolve new channels in small daily batches
  (tens/hour max), never on-demand-per-token. Pre-discover names via tgstat/Google.
- Aged account >> fresh; real SIM >> VoIP. Strictly read-only (no sends/joins/contacts)
  keeps ban risk ~zero. FloodWaitError → sleep exactly the given seconds, never early.
- Join cap (if ever needed): 500 channels (1,000 Premium).

## Where alpha ORIGINATES vs amplifies (the chain)

pump.fun livestream/dev action → private paid TG/Discord → X KOLs/Communities → **public
TG call channels** → listicle/aggregator amplification.

Public TG sits nearer the AMPLIFICATION end — consistent with H2 risk. The study measures
this per-channel rather than assuming it. Earlier-chain sources already in our study:
pump.fun dev actions (cf. dev-buyback edge memory) + Dexscreener paid-promo orders.
X-scraping feasibility = separate task, only if lead-time thesis survives Phase 1.

## Study seed list (Phase 1)

Start with all 16 Tier A. Liveness-check each via `t.me/s/<handle>` (HTTP, free) before
Telethon resolution; drop dead ones; resolve survivors in 2-3 daily batches.
