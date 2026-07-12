# Info-Source Bot — Phase 0 Source Inventory

_2026-07-11. Plan: `~/.claude/plans/logical-scribbling-kernighan.md` (H1 boards-lead-wallets
vs H2 wallets-are-the-source; nothing trades until a source proves H1)._

## Verified access (probed 2026-07-11, all headless unless noted)

| # | Source | Endpoint | Status | Retro? | Notes |
|---|--------|----------|--------|--------|-------|
| 1 | **Dexscreener orders** (paid promo per token) | `GET api.dexscreener.com/orders/v1/solana/{token}` | ✅ 200 | **YES** | Returns `paymentTimestamp` per paid `tokenProfile` / `communityTakeover` / boost. Timestamped "someone paid to promote this" — plugs straight into Phase 1. Verified on manlet: profile paid 06-29, CTO 07-01 (right at its runs). |
| 2 | Dexscreener boosts feed | `GET .../token-boosts/latest/v1`, `/top/v1` | ✅ 200 | live-fwd | 30-token list, no timestamp in feed → poll + log (Phase 2 collector). |
| 3 | **pump.fun coin detail** | `GET frontend-api-v3.pump.fun/coins/{mint}` | ✅ 200 | **YES** | `created_timestamp`, `creator`, `complete` (graduated). Existing patterns in `scripts/pumpfun_lifecycle_analysis.py`. |
| 4 | 4chan /biz/ live | `GET a.4cdn.org/biz/catalog.json` | ✅ 200 | live-fwd | Public JSON API, no auth. |
| 5 | **warosu /biz/ archive** | `GET warosu.org/biz/?task=search2&search_text={CA}` | ✅ 200 | **YES** | Full-text CA search over /biz/ history. HTML parse. |
| 6 | **Telegram channels** | Telethon MTProto (read-only) | ⏳ needs session | **YES** | THE main source. Public channel history is retro-searchable. Setup: `scripts/tg_session_setup.py` (Roy, ~5 min, my.telegram.org api_id/hash). telethon==1.44.0. Session file gitignored (`*.session`). |
| 7 | GMGN trending | `gmgn.ai/defi/quotation/...` | ❌ 403 WAF | — | Cloudflare-blocked headless. Attended-browser only → deprioritized. |
| 8 | Photon / BullX | — | not probed | — | Expect WAF like GMGN. Deprioritized for study; revisit if TG signal weak. |
| 9 | Discord alpha servers | — | skipped | — | Invite-gated + ToS. Phase 2+ only if evidence demands. |
| 10 | X/Twitter | — | skipped | — | Late-stage amplification (Roy). Old EVENT design's $100/mo API avoided. |

## Runner corpus available for Phase 1 (from live DB, 2026-07-11)

- `prerun_scans`: **98 runner-tokens** scanned (run_x ≥ 3), 2026-05-19 → 2026-07-09.
- `prerun_accumulators`: **34 tokens** with per-wallet `first_buy` timestamps → the
  H1/H2 discriminator arm (source-mention vs earliest smart-wallet buy).
- All 98 usable for source-vs-run_start lead stats. `run_date` is day-granular (1D
  candle detection) → Phase 1 re-derives intraday run-start via Birdeye `history_price`
  1H candles around run_date.
- Corpus grows daily (04:10 cron); Dune `runner_scan_v2.sql` (validated) can backfill
  more months if N needs a boost.

## TG channel candidate list

See companion file `research/tg_channel_candidates_2026-07-11.md` (from the channel-
discovery research pass). Channels are CANDIDATES until the study ranks them on their
own hit-rate; garbage channels get dropped by measurement, not by reputation.

## Phase 1 design notes

- Per runner × per source: first-mention ts → compare vs intraday run_start AND vs
  `min(first_buy)` of genuine accumulators (34-token arm), plus vetted-wallet buys in
  `wallet_swaps_log` where overlapping.
- TG first-mention: Telethon `client.iter_messages(channel, search=CA)` per channel ×
  per token; ~1 req/2.5s + FloodWait backoff. 30 channels × 98 tokens ≈ 3k searches ≈
  2-3h wall-clock, one evening run.
- Also parse `$TICKER` mentions as fallback (callers sometimes omit the CA); accept misses.
- Pre-registered gates: in the plan file. Decide per-source, not pooled.
