-- Swing-copy feasibility probe: reconstruct (wallet, token) positions from wallet_swaps_log
-- and count how many exhibit multi-day accumulate->distribute behavior (the swing profile).
-- Pure DB, no Birdeye cost. First-pass position approximation (one aggregate per wallet+token).

WITH pos AS (
  SELECT wallet_address, token_mint,
         MIN(event_at) FILTER (WHERE side='buy')  AS first_buy,
         MAX(event_at)                             AS last_evt,
         SUM(notional_usd) FILTER (WHERE side='buy')  AS buy_usd,
         SUM(notional_usd) FILTER (WHERE side='sell') AS sell_usd,
         COUNT(*) FILTER (WHERE side='buy')  AS n_buys,
         COUNT(*) FILTER (WHERE side='sell') AS n_sells,
         EXTRACT(EPOCH FROM (MAX(event_at) - MIN(event_at) FILTER (WHERE side='buy')))/3600.0 AS span_h
  FROM wallet_swaps_log
  GROUP BY wallet_address, token_mint
)
SELECT
  'ALL positions'                                  AS bucket,
  COUNT(*)                                         AS n_pos,
  COUNT(DISTINCT wallet_address)                   AS n_wallets
FROM pos WHERE first_buy IS NOT NULL
UNION ALL
SELECT 'meaningful (buy>=500, n_buys>=2)', COUNT(*), COUNT(DISTINCT wallet_address)
FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
UNION ALL
SELECT 'swing >=12h span + distributed>=50%', COUNT(*), COUNT(DISTINCT wallet_address)
FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
  AND span_h>=12 AND sell_usd>=0.5*buy_usd
UNION ALL
SELECT 'swing >=24h span + distributed>=50%', COUNT(*), COUNT(DISTINCT wallet_address)
FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
  AND span_h>=24 AND sell_usd>=0.5*buy_usd
UNION ALL
SELECT 'swing >=48h span + distributed>=50%', COUNT(*), COUNT(DISTINCT wallet_address)
FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
  AND span_h>=48 AND sell_usd>=0.5*buy_usd;
