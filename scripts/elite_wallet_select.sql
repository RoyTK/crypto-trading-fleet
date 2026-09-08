-- Phase 1a: select elite forensic targets from wallet_swaps_log (no API cost).
-- Realized proxy = total sold - total bought; style indicators = win%, concentration,
-- deployed capital, activity, avg hold. Pick wallets spanning DIFFERENT styles.
WITH t AS (
  SELECT wallet_address, token_mint,
    SUM(notional_usd) FILTER (WHERE side='buy')  AS bought,
    SUM(notional_usd) FILTER (WHERE side='sell') AS sold,
    COUNT(*) AS swaps,
    COUNT(*) FILTER (WHERE side='buy') AS n_buys,
    MIN(event_at) FILTER (WHERE side='buy') AS first_buy,
    MAX(event_at) AS last_evt
  FROM wallet_swaps_log GROUP BY 1,2
),
w AS (
  SELECT wallet_address,
    COUNT(*) AS tokens,
    COUNT(*) FILTER (WHERE COALESCE(sold,0) > COALESCE(bought,0)) AS win_tokens,
    ROUND(SUM(COALESCE(sold,0)-COALESCE(bought,0))::numeric,0) AS realized,
    ROUND(MAX(COALESCE(sold,0)-COALESCE(bought,0))::numeric,0) AS top_token_pnl,
    ROUND(SUM(COALESCE(bought,0))::numeric,0) AS deployed,
    SUM(swaps) AS swaps,
    ROUND(AVG(n_buys)::numeric,1) AS avg_buys_per_tok,
    ROUND(percentile_cont(0.5) WITHIN GROUP (
      ORDER BY EXTRACT(EPOCH FROM (last_evt - first_buy))/3600.0)::numeric,1) AS med_span_h
  FROM t WHERE first_buy IS NOT NULL GROUP BY 1
)
SELECT substring(w.wallet_address,1,8) AS wallet, wp.tier,
  w.tokens, w.win_tokens,
  ROUND((w.win_tokens::float/NULLIF(w.tokens,0)*100)::numeric,0) AS win_pct,
  w.realized, w.top_token_pnl,
  ROUND((w.top_token_pnl::float/NULLIF(w.realized,0)*100)::numeric,0) AS top_conc_pct,
  w.deployed, w.swaps, w.avg_buys_per_tok, w.med_span_h
FROM w JOIN wallet_pool wp ON wp.address = w.wallet_address
WHERE w.realized > 60000 AND w.tokens >= 8
ORDER BY w.realized DESC LIMIT 18;
