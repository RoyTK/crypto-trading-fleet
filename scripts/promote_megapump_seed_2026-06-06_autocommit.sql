-- Same changes as promote_megapump_seed_2026-06-06.sql but autocommit (no BEGIN/COMMIT).
-- Use this if the original was rolled back due to PSQL's open-transaction-on-EOF behavior.
-- Each statement commits on its own; failure of one does not roll back prior ones.

-- 1) DEMOTE active HFT → watch
UPDATE wallet_pool
SET tier = 'watch', demoted_at = NOW()
WHERE address IN (
  'CreQJ2t94QK5dsxUZGXfPJ8Nx7wA9LHr5chxjSMkbNft',
  'FrhLfj81LRpMR3EwSSCGPzHmxsnoq8h628ne68vK5oMN'
) AND tier = 'active';

-- 2) DEMOTE watch HFT → pruned
UPDATE wallet_pool
SET tier = 'pruned', demoted_at = NOW()
WHERE address = 'FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp'
  AND tier IN ('active', 'watch');

-- 3) PROMOTE 9ZPsRWG → active (pinned)
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

-- 4) WATCH tier inserts
INSERT INTO wallet_pool (address, chain, tier, source, added_at) VALUES
  ('28RWPC6XPxSxUVjd27KiNRtcHVxKtd6cjms7eBcxiqdV', 'solana', 'watch',
   'browser_opus_megapump_2026-06-06', NOW())
ON CONFLICT (address) DO NOTHING;

INSERT INTO wallet_pool (address, chain, tier, source, added_at) VALUES
  ('DSFWxvJjgvLbVqoQ1Zwoe1iVHhwFgrfSiF3ZK1h7nd1K', 'solana', 'watch',
   'browser_opus_megapump_2026-06-06', NOW())
ON CONFLICT (address) DO NOTHING;
