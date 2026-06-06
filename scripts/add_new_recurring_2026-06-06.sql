-- 2 NEW cross-token recurring wallets surfaced from browser-Opus's
-- PYTHIA/arc/ALCH leading-indicator analysis 2026-06-06.
-- Both appear in arc + ALCH (2-token recurrence). No Solscan whale tag
-- mentioned, no deep-dive yet — parking at watch tier per our framework
-- (watch is the right pre-validation parking spot; cluster-promotion rule
-- will lift them to active if real signal emerges).

INSERT INTO wallet_pool (address, chain, tier, source, added_at) VALUES
  ('g848qLVXs2rQcRXBcK3nSEHCdhW5rUyBkX4Loh7p2ma', 'solana', 'watch',
   'browser_opus_leading_indicator_2026-06-06', NOW())
ON CONFLICT (address) DO NOTHING;

INSERT INTO wallet_pool (address, chain, tier, source, added_at) VALUES
  ('A9PzVmoBvTMLSh8VcaPQfYc3qwH63pmLuMxRmWraqGYk', 'solana', 'watch',
   'browser_opus_leading_indicator_2026-06-06', NOW())
ON CONFLICT (address) DO NOTHING;

-- Verify
SELECT address, tier, source FROM wallet_pool
WHERE address IN (
  'g848qLVXs2rQcRXBcK3nSEHCdhW5rUyBkX4Loh7p2ma',
  'A9PzVmoBvTMLSh8VcaPQfYc3qwH63pmLuMxRmWraqGYk'
);
