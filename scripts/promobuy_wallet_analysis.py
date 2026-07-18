"""Promobuy exploratory analysis (Roy 2026-07-18): find recurring wallets in promo
tokens (we can't follow snipers IN, but they may signal when to get OUT), and profile
whether the entry-time features predict the token's trajectory (fast-rug / slow-bleed /
run / longer-promo-more-run). Many angles. Read-only. Runs in bot_copy.

CAVEAT surfaced first: wallet_swaps_log only holds OUR subscribed wallets (active/watch/
teamfollow) — so 'recurring wallets in promo tokens' = which of OUR roster wallets also
play promoted tokens, NOT arbitrary snipers. The true early-sniper behavior lives in the
signal_features first-buyer snapshot (Birdeye), covered in the profile cuts.
"""
from collections import defaultdict

from sqlalchemy import text
from framework.db import session_scope


def q(s, sql):
    return list(s.execute(text(sql)).mappings())


PTOK = "SELECT DISTINCT asset FROM trades WHERE bot_id='copy' AND sim_metadata->>'strategy' LIKE 'promobuy%'"


def section(title):
    print(f"\n{'='*72}\n{title}\n{'='*72}")


def main():
    with session_scope() as s:
        # ---- 0. overlap: do our subscribed wallets even touch promo tokens? ----
        section("0. OVERLAP — promo tokens vs our subscribed-wallet swap log")
        r = q(s, f"""
          WITH p AS ({PTOK})
          SELECT (SELECT count(*) FROM p) AS promo_tokens,
                 (SELECT count(DISTINCT token_mint) FROM wallet_swaps_log w WHERE w.token_mint IN (SELECT asset FROM p)) AS with_wallet_activity,
                 (SELECT count(*) FROM wallet_swaps_log w WHERE w.token_mint IN (SELECT asset FROM p)) AS total_swaps
        """)[0]
        print(f"  promo tokens: {r['promo_tokens']}   with any roster-wallet activity: {r['with_wallet_activity']}   total swaps logged: {r['total_swaps']}")

        # ---- 1. recurring wallets across promo tokens ----
        section("1. RECURRING WALLETS across promo tokens (>=3 tokens) — candidates to 'follow out'")
        rows = q(s, f"""
          WITH p AS ({PTOK})
          SELECT substr(w.wallet_address,1,10) AS wallet, w.source_webhook AS tier,
                 count(DISTINCT w.token_mint) AS n_tokens,
                 count(*) FILTER (WHERE w.side='buy') AS buys,
                 count(*) FILTER (WHERE w.side='sell') AS sells,
                 round(avg(w.notional_usd)::numeric,0) AS avg_usd
          FROM wallet_swaps_log w WHERE w.token_mint IN (SELECT asset FROM p)
          GROUP BY 1,2 HAVING count(DISTINCT w.token_mint) >= 3
          ORDER BY n_tokens DESC LIMIT 25
        """)
        print(f"  {'wallet':>11} {'tier':>10} {'tokens':>6} {'buys':>5} {'sells':>5} {'avg$':>7}")
        for x in rows:
            print(f"  {x['wallet']:>11} {x['tier']:>10} {x['n_tokens']:>6} {x['buys']:>5} {x['sells']:>5} {x['avg_usd']:>7}")

        # ---- 2. trajectory classes + first-buyer profile ----
        section("2. TRAJECTORY x ENTRY PROFILE (does the entry snapshot predict fast-rug/slow/run?)")
        rows = q(s, """
          WITH t AS (
            SELECT tr.id, tr.pnl_usd p, tr.size_usd sz,
              COALESCE((tr.sim_metadata->>'peak_pct_since_entry')::float,0) peak,
              EXTRACT(EPOCH FROM (tr.exit_at - tr.entry_at))/60 hold_min, sf.features_json f
            FROM trades tr JOIN signal_features sf ON sf.trade_id=tr.id
            WHERE tr.bot_id='copy' AND tr.sim_metadata->>'strategy' LIKE 'promobuy%'
              AND tr.fill_status='closed' AND sf.features_json->>'first_buyer_bundler_frac' IS NOT NULL)
          SELECT CASE WHEN peak>=100 THEN 'run (2x+)'
                      WHEN peak<30 THEN 'fast-rug (<30% peak)'
                      WHEN p<0 THEN 'pumped-then-died'
                      ELSE 'small win/flat' END AS cls,
            count(*) n, round(avg(hold_min)::numeric,0) hold_min, round(avg(p)::numeric,0) avg_pnl,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'first_buyer_sniper_frac')::float)::numeric,2) sniper,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'first_buyer_sell_all_frac')::float)::numeric,2) sell_all,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'first_buyer_hold_frac')::float)::numeric,2) hold_f,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'first_buyer_top5_vol_frac')::float)::numeric,2) top5,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'liq_mcap_ratio')::float)::numeric,3) liq_mcap,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'buy_sell_ratio_h1')::float)::numeric,2) bsr,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'boost_amount')::float)::numeric,0) boost,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'market_cap')::float)::numeric,0) mcap
          FROM t GROUP BY 1 ORDER BY avg_pnl
        """)
        cols = ['cls','n','hold_min','avg_pnl','sniper','sell_all','hold_f','top5','liq_mcap','bsr','boost','mcap']
        print("  " + " ".join(f"{c:>10}" for c in cols))
        for x in rows:
            print("  " + " ".join(f"{str(x[c]):>10}" for c in cols))

        # ---- 3. promotion magnitude x outcome ----
        section("3. PROMO MAGNITUDE (boost size / re-promotion) x outcome — 'more/longer promo, more run'?")
        rows = q(s, """
          SELECT CASE WHEN COALESCE(boost_amount,0)=0 THEN 'a. no boost'
                      WHEN boost_amount<30 THEN 'b. boost <30'
                      WHEN boost_amount<100 THEN 'c. boost 30-99'
                      ELSE 'd. boost >=100' END AS boost_band,
            count(*) FILTER (WHERE fwd_mult_max IS NOT NULL) n,
            round(avg(fwd_mult_max)::numeric,2) avg_fwd_max,
            round((100.0*count(*) FILTER (WHERE fwd_mult_max>=2)/NULLIF(count(*) FILTER (WHERE fwd_mult_max IS NOT NULL),0))::numeric,0) pct_2x,
            round(avg(resignal_count)::numeric,2) avg_resignals,
            round((100.0*count(*) FILTER (WHERE fwd_mult_min<0.1)/NULLIF(count(*) FILTER (WHERE fwd_mult_min IS NOT NULL),0))::numeric,0) pct_rug90
          FROM promo_shadow_signals GROUP BY 1 ORDER BY 1
        """)
        for x in rows:
            print(f"  {x['boost_band']:<16} n={x['n']:>4}  avg_fwd_max={x['avg_fwd_max']}x  2x={x['pct_2x']}%  rug={x['pct_rug90']}%  avg_resignals={x['avg_resignals']}")
        print("  (re-promotion = resignal_count: did tokens that got RE-boosted run more?)")
        rows = q(s, """
          SELECT CASE WHEN COALESCE(resignal_count,0)=0 THEN 'promoted once' ELSE 'RE-promoted (>=1 resignal)' END AS reprom,
            count(*) FILTER (WHERE fwd_mult_max IS NOT NULL) n,
            round(avg(fwd_mult_max)::numeric,2) avg_fwd_max,
            round((100.0*count(*) FILTER (WHERE fwd_mult_max>=2)/NULLIF(count(*) FILTER (WHERE fwd_mult_max IS NOT NULL),0))::numeric,0) pct_2x
          FROM promo_shadow_signals GROUP BY 1 ORDER BY 1
        """)
        for x in rows:
            print(f"  {x['reprom']:<26} n={x['n']:>4}  avg_fwd_max={x['avg_fwd_max']}x  2x={x['pct_2x']}%")

        # ---- 4. did a roster wallet SELL before our exit on promobuy losers? (follow-out test) ----
        section("4. FOLLOW-OUT TEST — on promobuy trades, did a roster wallet SELL during our hold, and what happened?")
        rows = q(s, """
          WITH tr AS (
            SELECT id, asset, entry_at, exit_at, pnl_usd, size_usd,
              COALESCE((sim_metadata->>'peak_pct_since_entry')::float,0) peak
            FROM trades WHERE bot_id='copy' AND sim_metadata->>'strategy' LIKE 'promobuy%'
              AND fill_status='closed' AND exit_at IS NOT NULL),
          e AS (
            SELECT tr.*,
              (SELECT count(*) FROM wallet_swaps_log w WHERE w.token_mint=tr.asset AND w.side='sell'
                 AND w.event_at BETWEEN tr.entry_at AND tr.exit_at) AS roster_sells_during,
              (SELECT count(*) FROM wallet_swaps_log w WHERE w.token_mint=tr.asset AND w.side='buy'
                 AND w.event_at BETWEEN tr.entry_at AND tr.exit_at) AS roster_buys_during
            FROM tr)
          SELECT CASE WHEN roster_sells_during>0 THEN 'roster SOLD during our hold' ELSE 'no roster sell' END AS grp,
            count(*) n, round(avg(pnl_usd)::numeric,0) avg_pnl,
            round((100.0*count(*) FILTER (WHERE pnl_usd>0)/count(*))::numeric,0) win_pct,
            round(avg(roster_buys_during)::numeric,1) avg_buys_during
          FROM e GROUP BY 1 ORDER BY 1
        """)
        for x in rows:
            print(f"  {x['grp']:<28} n={x['n']:>4}  avg_pnl=${x['avg_pnl']}  win={x['win_pct']}%  avg_roster_buys_during={x['avg_buys_during']}")


if __name__ == "__main__":
    main()
