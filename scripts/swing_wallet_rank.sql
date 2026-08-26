-- Per-wallet swing ranking (Track 2 discovery seed) + Birdeye job sizing.
-- Swing position = buy>=500, n_buys>=2, span>=24h, distributed>=50% (first-pass aggregate).

WITH pos AS (
  SELECT wallet_address, token_mint,
         MIN(event_at) FILTER (WHERE side='buy')  AS first_buy,
         MAX(event_at)                             AS last_evt,
         SUM(notional_usd) FILTER (WHERE side='buy')  AS buy_usd,
         SUM(notional_usd) FILTER (WHERE side='sell') AS sell_usd,
         COUNT(*) FILTER (WHERE side='buy')  AS n_buys,
         EXTRACT(EPOCH FROM (MAX(event_at) - MIN(event_at) FILTER (WHERE side='buy')))/3600.0 AS span_h
  FROM wallet_swaps_log
  GROUP BY wallet_address, token_mint
),
swing AS (
  SELECT * FROM pos
  WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
    AND span_h>=24 AND sell_usd>=0.5*buy_usd
)
SELECT
  substring(s.wallet_address,1,8) AS wallet,
  wp.tier, wp.source,
  COUNT(*)                         AS swing_pos,
  COUNT(DISTINCT s.token_mint)     AS tokens,
  ROUND(SUM(s.buy_usd)::numeric,0) AS buy_usd,
  ROUND((SUM(s.sell_usd)-SUM(s.buy_usd))::numeric,0) AS crude_realized,
  ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY s.span_h)::numeric,1) AS med_span_h
FROM swing s
LEFT JOIN wallet_pool wp ON wp.address = s.wallet_address
GROUP BY 1,2,3
HAVING COUNT(*) >= 5
ORDER BY crude_realized DESC
LIMIT 40;
