-- Promotion / demotion plan from browser-Opus 2026-06-06 Solscan deep-dive.
-- Based on per-wallet behavior verification (Cielo couldn't validate this
-- cohort — most appeared as "tracked but zero swaps" because they trade
-- via DEX aggregators that Cielo doesn't fully index).
--
-- Run inside a transaction; review row counts; commit/rollback.
--
-- Cost: zero. Pure pool-tier reassignment.
-- Operational impact: bot_copy reloads wallet_pool every 60s, so the active
-- subscriber set updates automatically — no restart needed.

BEGIN;

-- ============================================================================
-- 1) DEMOTE the 3 HFT-tagged in-pool wallets (Solscan + browser-Opus consensus)
-- ============================================================================
-- CreQJ2t9: HFT-tagged on ZEREBRO + GRIFFAIN. 58 paper trades, avg -0.2% PnL.
-- FrhLfj81: HFT-tagged on ACT. 8 paper trades, avg -0.4% PnL.
-- FkaLnX17: HFT-tagged on ZEREBRO. Already watch tier; pin demotion permanent.
--
-- These wallets fire alongside real signal but their independent picks lose
-- money. They pollute the cluster signal with false-recurrence noise.

UPDATE wallet_pool
SET tier = 'watch',
    demoted_at = NOW()
WHERE address IN (
  'CreQJ2t94QK5dsxUZGXfPJ8Nx7wA9LHr5chxjSMkbNft',
  'FrhLfj81LRpMR3EwSSCGPzHmxsnoq8h628ne68vK5oMN'
) AND tier = 'active';

-- FkaLnX17 already at watch; move to pruned (it's HFT + already demoted once)
UPDATE wallet_pool
SET tier = 'pruned',
    demoted_at = NOW()
WHERE address = 'FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp'
  AND tier IN ('active', 'watch');


-- ============================================================================
-- 2) PROMOTE 9ZPsRWG to active (THE clear winner)
-- ============================================================================
-- Browser-Opus 2026-06-06 deep-dive: pure-meme HFT swing desk, 94.6% meme
-- portfolio, last swap 48 seconds before scrape, 10 swaps in 14 minutes via
-- Raydium/Jupiter. Holds 4 of our 15 historical pumps (TROLL, Fartcoin,
-- pippin, swarms) AND its current top 3 positions are PYTHIA / arc / ALCH —
-- potential leading indicators for the next mega-pumps.
--
-- Pinned because the promotion rationale comes from an off-Cielo data source
-- (Solscan portfolio + manual whale-tag verification); we don't want the
-- 30-day-events demotion rule from the daily cron to kick this back to watch
-- before we've collected attribution data on its picks.

INSERT INTO wallet_pool (
  address, chain, tier, source,
  added_at, promoted_at,
  pinned, pinned_at, pinned_reason
) VALUES (
  '9ZPsRWGkukYeWg2Z7eZ8NaTBZ1DSuBUVzLcGQWZgE4Y4',
  'solana', 'active',
  'browser_opus_megapump_2026-06-06',
  NOW(), NOW(),
  TRUE, NOW(),
  'Solscan deep-dive 2026-06-06: pure meme HFT desk (94.6% meme), active now '
  || '(swap 48s ago), in 5 historical pumps (GOAT/ZEREBRO/FARTCOIN/GRIFFAIN/PIPPIN). '
  || 'Top current positions PYTHIA/arc/ALCH = leading-indicator candidates. '
  || 'Cielo had this wallet as "not tracked" but Solscan confirms active trading.'
) ON CONFLICT (address) DO UPDATE
SET tier = 'active', promoted_at = NOW(), pinned = TRUE, pinned_at = NOW(),
    pinned_reason = EXCLUDED.pinned_reason;


-- ============================================================================
-- 3) WATCH tier for the two ambiguous candidates
-- ============================================================================
-- 28RWPC6X: active meme whale in 4 historical pumps + jellyjelly/WhiteWhale
-- candidates, but recent activity is TRANSFER-dominated (distribution node
-- pattern). Park at watch tier; if it fires alongside active wallets in a
-- real cluster, the existing watch->active promotion rule (5+ swaps in 7d
-- with WR>=55%) will pull it through.
--
-- DSFWxvJj: slow-cadence meme+defi holder. Last tx 5h ago. Moderate signal.
-- Same watch-tier parking logic.

INSERT INTO wallet_pool (
  address, chain, tier, source, added_at
) VALUES
  ('28RWPC6XPxSxUVjd27KiNRtcHVxKtd6cjms7eBcxiqdV', 'solana', 'watch',
   'browser_opus_megapump_2026-06-06', NOW()),
  ('DSFWxvJjgvLbVqoQ1Zwoe1iVHhwFgrfSiF3ZK1h7nd1K', 'solana', 'watch',
   'browser_opus_megapump_2026-06-06', NOW())
ON CONFLICT (address) DO NOTHING;


-- ============================================================================
-- 4) DROP candidates: 2Ejnns2 (diversified fund) + 7w2UAHpF (cash mgmt)
-- ============================================================================
-- 2Ejnns2: $67M, 69.6% stablecoin, blue-chip portfolio. Not pump-chaser.
-- 7w2UAHpF: CoinEx-funded 2 days ago, 77% USDT, all-transfer activity. The
--   "persistence into 2026" via memecoin holding was misleading — not a
--   conviction trade, just cash mgmt. The persistence finding from 2026-06-05
--   is RETRACTED based on this evidence.
--
-- Not inserted into pool (no row = not tracked).


-- ============================================================================
-- Verify before commit
-- ============================================================================
SELECT 'After tier changes:' AS check;
SELECT tier, COUNT(*) FROM wallet_pool GROUP BY tier ORDER BY tier;

SELECT 'Promoted/demoted in this transaction:' AS check;
SELECT address, tier, source, pinned, pinned_reason
FROM wallet_pool
WHERE address IN (
  'CreQJ2t94QK5dsxUZGXfPJ8Nx7wA9LHr5chxjSMkbNft',
  'FrhLfj81LRpMR3EwSSCGPzHmxsnoq8h628ne68vK5oMN',
  'FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp',
  '9ZPsRWGkukYeWg2Z7eZ8NaTBZ1DSuBUVzLcGQWZgE4Y4',
  '28RWPC6XPxSxUVjd27KiNRtcHVxKtd6cjms7eBcxiqdV',
  'DSFWxvJjgvLbVqoQ1Zwoe1iVHhwFgrfSiF3ZK1h7nd1K'
);

-- COMMIT or ROLLBACK based on review
-- COMMIT;
