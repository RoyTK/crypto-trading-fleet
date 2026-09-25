"""Generate the fleet's Grafana dashboards (v2) from one template.

Why a generator: the hand-edited dashboards drifted apart (different layouts, refresh,
time ranges, stale panels). Every page is now built from the same building blocks:
  - Fleet Command (home)          uid fleet-overview
  - one page per strategy          shared CORE + per-strategy MODULES
  - Ops & Health                   uid ops-health (embeds the service-usage panels)
  - Wallet Pool                    uid wallet-pool

Conventions: timezone America/Chicago; stat strips = CURRENT ERA (since the strategy's
last reset re-tag); charts/tables follow the time picker ($__timeFilter); markers from
strategy_changes + halts; open positions carry CURRENT liquidity (position_liquidity_log)
and are marked RUG at -100% when liquidity < $100.

Usage:  python scripts/build_dashboards.py [--out monitoring/dashboards]
Then validate on the server: python scripts/validate_dashboards.py <files>
"""
from __future__ import annotations

import argparse
import json
import os

DS = {"type": "postgres", "uid": "fleet-postgres"}
TZ = "America/Chicago"
NAV_TAG = "fleet-v2"
RUG_USD = 100          # current liquidity below this = rug (pulled pools read ~$0.000001)
COLLAPSE_FRAC = 0.2    # current liq < 20% of entry liq = LIQ COLLAPSED
STRATS = ["cluster", "conviction", "swing", "teamfollow", "cohortfire", "promobuy"]
DORMANT_BY_DESIGN = {"cohortfire"}   # not flagged as "idle" on the attention strip
DEFAULT_RANGE = {"conviction": "now-90d", "cohortfire": "now-90d"}  # paused -> show their last trades
SCORECARD_H = 9       # grid rows; must show all strategies without scrolling (verified by screenshot)


def halt_id(s: str) -> str:
    return "copy" if s == "cluster" else f"copy_{s}"


def in_list(items) -> str:
    return ",".join(f"'{x}'" for x in items)


def closed(s: str, alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return (f"{a}bot_id='copy' AND {a}mode='paper' AND {a}fill_status='closed' "
            f"AND {a}sim_metadata->>'strategy'='{s}'")


def family(s: str, alias: str = "t") -> str:
    """Current tag + its retired eras (for before/after-change comparisons)."""
    return (f"({alias}.sim_metadata->>'strategy'='{s}' OR "
            f"{alias}.sim_metadata->>'strategy' LIKE '{s}\\_pre\\_reset%')")


# ---------------------------------------------------------------- open positions ----
def open_cte(strategies=None) -> str:
    """CTE `o`: every open paper position with current liquidity, status, rug-marked unrealized."""
    filt = f"AND t.sim_metadata->>'strategy' IN ({in_list(strategies)})" if strategies else ""
    return f"""o AS (
  SELECT t.id, t.asset, t.entry_at, t.sim_metadata->>'strategy' AS strategy,
    COALESCE(t.sim_metadata->>'trigger_wallet', t.sim_metadata->>'team_id', '') AS who,
    (t.sim_metadata->>'peak_pct_since_entry')::numeric AS peak_pct,
    (t.sim_metadata->>'entry_liquidity_usd')::numeric AS el,
    COALESCE((t.sim_metadata->>'remaining_size_usd')::numeric, t.size_usd::numeric) AS rem,
    t.entry_price::numeric AS ep, l.liquidity_usd::numeric AS liq, l.price::numeric AS px, l.logged_at,
    CASE WHEN l.logged_at IS NULL THEN 'NO DATA'
         WHEN l.liquidity_usd < {RUG_USD} THEN 'RUG'
         WHEN l.logged_at < now() - interval '2 hours' THEN 'STALE'
         WHEN (t.sim_metadata->>'entry_liquidity_usd')::numeric > 0
              AND l.liquidity_usd < {COLLAPSE_FRAC} * (t.sim_metadata->>'entry_liquidity_usd')::numeric
              THEN 'LIQ COLLAPSED'
         ELSE 'ok' END AS status
  FROM trades t
  LEFT JOIN LATERAL (SELECT liquidity_usd, price, logged_at FROM position_liquidity_log p
                     WHERE p.trade_id = t.id ORDER BY logged_at DESC LIMIT 1) l ON true
  WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='open' {filt}
), ou AS (
  SELECT o.*, CASE WHEN status='RUG' THEN -rem
                   WHEN px IS NOT NULL AND ep > 0 THEN rem * (px / ep - 1) END AS unreal
  FROM o)"""


def open_positions_sql(strategies=None) -> str:
    return f"""WITH {open_cte(strategies)}
SELECT id, strategy, asset, status, ROUND(rem,0) AS size_usd, ROUND(unreal,0) AS unreal_usd,
  ROUND((EXTRACT(EPOCH FROM now()-entry_at)/3600)::numeric,1) AS held_h,
  ROUND(el,0) AS entry_liq_usd, ROUND(liq,0) AS liq_now_usd,
  ROUND((liq / NULLIF(el,0) - 1) * 100, 0) AS liq_chg_pct,
  ROUND((EXTRACT(EPOCH FROM now()-logged_at)/60)::numeric,0) AS liq_age_min,
  ROUND(peak_pct,1) AS peak_pct, LEFT(who,10) AS trigger, entry_at
FROM ou
ORDER BY CASE status WHEN 'RUG' THEN 0 WHEN 'LIQ COLLAPSED' THEN 1 WHEN 'STALE' THEN 2
         WHEN 'NO DATA' THEN 3 ELSE 4 END, unreal NULLS FIRST"""


# ------------------------------------------------------------------ panel helpers ----
_id = [0]


def pid() -> int:
    _id[0] += 1
    return _id[0]


def tgt(sql: str, fmt: str = "table") -> list:
    return [{"refId": "A", "datasource": DS, "format": fmt, "rawQuery": True,
             "editorMode": "code", "rawSql": sql}]


STATE_MAPPINGS = [
    {"type": "regex", "options": {"pattern": "^HALTED.*", "result": {"color": "red", "index": 0}}},
    {"type": "value", "options": {"running": {"color": "green", "index": 1}}},
]

USD_TH = {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0}]}

TABLE_OVERRIDES = [
    {"matcher": {"id": "byRegexp", "options": ".*_usd$"},
     "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 0},
                    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                    {"id": "thresholds", "value": USD_TH}]},
    {"matcher": {"id": "byRegexp", "options": ".*_pct$"},
     "properties": [{"id": "unit", "value": "percent"}]},
    {"matcher": {"id": "byName", "options": "asset"},
     "properties": [{"id": "links", "value": [{"title": "Birdeye", "targetBlank": True,
                    "url": "https://birdeye.so/solana/token/${__value.raw}"}]}]},
    {"matcher": {"id": "byName", "options": "wallet"},
     "properties": [{"id": "links", "value": [{"title": "Birdeye wallet", "targetBlank": True,
                    "url": "https://birdeye.so/solana/wallet-analyzer/${__value.raw}"}]}]},
    {"matcher": {"id": "byName", "options": "status"},
     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "mappings", "value": [{"type": "value", "options": {
                        "RUG": {"color": "red", "index": 0},
                        "LIQ COLLAPSED": {"color": "orange", "index": 1},
                        "STALE": {"color": "#6e6e6e", "index": 2},
                        "NO DATA": {"color": "#6e6e6e", "index": 3},
                        "ok": {"color": "green", "index": 4}}}]}]},
    {"matcher": {"id": "byName", "options": "sev"},
     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "custom.width", "value": 60},
                    {"id": "mappings", "value": [{"type": "value", "options": {
                        "P0": {"color": "red", "index": 0}, "P1": {"color": "orange", "index": 1},
                        "P2": {"color": "yellow", "index": 2}, "INFO": {"color": "blue", "index": 3},
                        "OK": {"color": "green", "index": 4}}}]}]},
    {"matcher": {"id": "byName", "options": "state"},
     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                    {"id": "mappings", "value": STATE_MAPPINGS}]},
]



EMPTY_TIME = "No trades in the selected time range (check the time picker, top right)"


def table(title, sql, desc=None, widths=None):
    ov = list(TABLE_OVERRIDES) + [
        {"matcher": {"id": "byName", "options": col}, "properties": [{"id": "custom.width", "value": w}]}
        for col, w in (widths or {}).items()]
    p = {"id": pid(), "type": "table", "title": title, "datasource": DS, "targets": tgt(sql),
         "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": False, "minWidth": 50}},
                         "overrides": ov},
         "options": {"showHeader": True, "cellHeight": "sm"}}
    if desc:
        p["description"] = desc
    return p


def stat(title, sql, unit=None, thresholds=None, mappings=None, desc=None):
    d = {}
    if unit:
        d["unit"] = unit
    if thresholds:
        d["thresholds"] = thresholds
        d["color"] = {"mode": "thresholds"}
    if mappings:
        d["mappings"] = mappings
        d.setdefault("color", {"mode": "thresholds"})
    if not thresholds and not mappings:
        d["color"] = {"mode": "fixed", "fixedColor": "text"}
    p = {"id": pid(), "type": "stat", "title": title, "datasource": DS, "targets": tgt(sql),
         "fieldConfig": {"defaults": d, "overrides": []},
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False},
                     "colorMode": "value", "graphMode": "none", "textMode": "value",
                     "justifyMode": "center"}}
    if desc:
        p["description"] = desc
    return p


def timeseries(title, sql, unit="currencyUSD", zero_line=True, desc=None):
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
              "spanNulls": True}
    d = {"unit": unit, "custom": custom, "noValue": EMPTY_TIME}
    if zero_line:
        custom["thresholdsStyle"] = {"mode": "line"}
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                        {"color": "#888888", "value": 0}]}
    p = {"id": pid(), "type": "timeseries", "title": title, "datasource": DS,
         "targets": tgt(sql, "time_series"), "fieldConfig": {"defaults": d, "overrides": []},
         "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                     "tooltip": {"mode": "multi"}}}
    if desc:
        p["description"] = desc
    return p


def text_panel(md):
    return {"id": pid(), "type": "text", "title": "", "transparent": True,
            "options": {"mode": "markdown", "content": md}}


def bar(title, sql, desc=None):
    """Horizontal-label bar chart: one bar per entity, value = net $, label carries N trades.
    Bars coloured red/green by net P&L."""
    p = {"id": pid(), "type": "barchart", "title": title, "datasource": DS, "targets": tgt(sql),
         "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 0, "thresholds": USD_TH, "noValue": EMPTY_TIME,
                                      "color": {"mode": "thresholds"},
                                      "custom": {"fillOpacity": 85, "lineWidth": 0}},
                         "overrides": []},
         "options": {"xField": "label", "colorByField": "net_usd", "orientation": "vertical",
                     "showValue": "never", "xTickLabelRotation": -50, "xTickLabelSpacing": 0,
                     "barWidth": 0.85, "groupWidth": 0.7, "stacking": "none",
                     "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                     "tooltip": {"mode": "single"}}}
    if desc:
        p["description"] = desc
    return p


def layout(rows):
    """rows = [[(panel, w, h), ...], ...] -> panels with gridPos."""
    out, y = [], 0
    for row in rows:
        x, hmax = 0, 0
        for p, w, h in row:
            p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
            out.append(p)
            x += w
            hmax = max(hmax, h)
        y += hmax
    return out


def annotations(strategies=None):
    sfilter = (f"AND strategy IN ({in_list(list(strategies) + ['fleet'])})" if strategies else "")
    hfilter = (f"AND bot_id IN ({in_list([halt_id(s) for s in strategies])})" if strategies else "")
    return {"list": [
        {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
         "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts",
         "type": "dashboard"},
        {"datasource": DS, "enable": True, "iconColor": "#5794F2", "name": "Changes",
         "target": {"refId": "Anno", "format": "table", "rawQuery": True, "editorMode": "code",
                    "rawSql": ("SELECT changed_at AS time, strategy || ': ' || description AS text, "
                               "kind AS tags FROM strategy_changes WHERE $__timeFilter(changed_at) "
                               f"{sfilter} ORDER BY changed_at")}},
        {"datasource": DS, "enable": True, "iconColor": "#F2495C", "name": "Halts",
         "target": {"refId": "Halt", "format": "table", "rawQuery": True, "editorMode": "code",
                    "rawSql": ("SELECT halted_at AS time, resumed_at AS timeend, "
                               "bot_id || ' halted: ' || halt_type AS text, 'halt' AS tags "
                               f"FROM halts WHERE $__timeFilter(halted_at) {hfilter} ORDER BY halted_at")}},
    ]}


def dashboard(uid, title, panels, tags, strategies=None, time_from="now-30d"):
    return {"uid": uid, "title": title, "schemaVersion": 39, "version": 1, "editable": True,
            "refresh": "1m", "timezone": TZ, "time": {"from": time_from, "to": "now"},
            "tags": [NAV_TAG] + tags, "annotations": annotations(strategies),
            "links": [{"type": "dashboards", "tags": [NAV_TAG], "asDropdown": False,
                       "title": "Pages", "includeVars": False, "keepTime": False, "icon": "external link"}],
            "templating": {"list": []}, "panels": panels}


# ------------------------------------------------------------------ shared blocks ----
def stat_strip(s):
    h = halt_id(s)
    cur = f"bot_id='copy' AND mode='paper' AND sim_metadata->>'strategy'='{s}'"
    return [
        (stat("State", f"SELECT COALESCE((SELECT 'HALTED · ' || halt_type FROM halts WHERE bot_id='{h}' "
                       f"AND resumed_at IS NULL ORDER BY halted_at DESC LIMIT 1), 'running') AS state",
              mappings=STATE_MAPPINGS), 3, 4),
        (stat("Era start", f"SELECT to_char(min(entry_at) AT TIME ZONE '{TZ}', 'YYYY-MM-DD') AS era "
                           f"FROM trades WHERE {cur}",
              ), 3, 4),
        (stat("N closed", f"SELECT count(*) FROM trades WHERE {closed(s)}"), 3, 4),
        (stat("Exp/trade", f"SELECT ROUND(avg(pnl_usd)::numeric, 2) FROM trades WHERE {closed(s)}",
              unit="currencyUSD", thresholds=USD_TH), 3, 4),
        (stat("Win rate", f"SELECT ROUND(avg((pnl_usd > 0)::int) * 100, 1) FROM trades WHERE {closed(s)}",
              unit="percent"), 3, 4),
        (stat("Net P&L", f"SELECT ROUND(COALESCE(sum(pnl_usd), 0)::numeric, 0) FROM trades WHERE {closed(s)}",
              unit="currencyUSD", thresholds=USD_TH), 3, 4),
        (stat("Open P&L", f"WITH {open_cte([s])} SELECT ROUND(COALESCE(sum(unreal), 0), 0) FROM ou",
              unit="currencyUSD", thresholds=USD_TH), 3, 4),
        (stat("Last entry", f"SELECT ROUND((EXTRACT(EPOCH FROM now() - max(entry_at)) / 3600)::numeric, 1) "
                                    f"FROM trades WHERE {cur}", unit="h",
              thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                        {"color": "yellow", "value": 24}]}), 3, 4),
    ]


def rolling_expectancy(strategies, window=30):
    return timeseries(
        f"Rolling expectancy — avg $ per trade over last {window} trades",
        f"""SELECT time, metric, value FROM (
  SELECT exit_at AS time, sim_metadata->>'strategy' AS metric,
    AVG(pnl_usd) OVER (PARTITION BY sim_metadata->>'strategy' ORDER BY exit_at
                       ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS value
  FROM trades WHERE bot_id='copy' AND mode='paper' AND fill_status='closed' AND exit_at IS NOT NULL
    AND sim_metadata->>'strategy' IN ({in_list(strategies)})) x
WHERE $__timeFilter(time) ORDER BY 1""",
        desc="Above the grey line = making money per trade right now. Blue markers = changes, red = halts.")


def pnl_distribution(s):
    return table("P&L per trade — distribution & tail (time range)", f"""WITH c AS (
  SELECT pnl_usd, pnl_pct FROM trades WHERE {closed(s)} AND $__timeFilter(exit_at)),
tot AS (SELECT count(*) n FROM c)
SELECT substr(b, 3) AS bucket, count(*) AS n, ROUND(100.0 * count(*) / NULLIF(max(tot.n), 0), 0) AS share_pct,
  ROUND(sum(pnl_usd)::numeric, 0) AS net_usd
FROM (SELECT CASE WHEN pnl_pct <= -90 THEN '1 <= -90% (rug-like)' WHEN pnl_pct <= -50 THEN '2 -90..-50%'
                  WHEN pnl_pct <= -20 THEN '3 -50..-20%' WHEN pnl_pct < 0 THEN '4 -20..0%'
                  WHEN pnl_pct < 20 THEN '5 0..+20%' WHEN pnl_pct < 100 THEN '6 +20..+100%'
                  WHEN pnl_pct < 400 THEN '7 +100..+400%' ELSE '8 >= +400%' END AS b, pnl_usd FROM c) x, tot
GROUP BY b ORDER BY b""",
        desc="Where the money is made and lost. If net_usd is carried by the top buckets, the strategy is tail-dependent.")


def exit_reasons(s):
    return table("Why trades close — exit reason × hold × P&L (time range)", f"""SELECT COALESCE(exit_reason, '?') AS exit_reason, count(*) AS n,
  ROUND(avg((pnl_usd > 0)::int) * 100) AS win_pct, ROUND(avg(pnl_usd)::numeric, 1) AS exp_usd,
  ROUND(sum(pnl_usd)::numeric, 0) AS net_usd,
  ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM exit_at - entry_at) / 3600)::numeric, 2) AS med_hold_h
FROM trades WHERE {closed(s)} AND $__timeFilter(exit_at)
GROUP BY 1 ORDER BY n DESC""")


def entry_conditions(s):
    return table("Entry conditions vs outcome — are we entering late? (time range)", f"""WITH c AS (
  SELECT pnl_usd, (sim_metadata->>'token_age_at_entry_hours')::float AS age,
         (sim_metadata->>'entry_liquidity_usd')::float AS liq
  FROM trades WHERE {closed(s)} AND $__timeFilter(exit_at)),
b AS (
  SELECT 'A token age' AS factor, CASE WHEN age IS NULL THEN '9 unknown' WHEN age < 0.25 THEN '1 <15m'
      WHEN age < 1 THEN '2 15m-1h' WHEN age < 6 THEN '3 1-6h' WHEN age < 24 THEN '4 6-24h'
      WHEN age < 168 THEN '5 1-7d' ELSE '6 >7d' END AS bk, pnl_usd FROM c
  UNION ALL
  SELECT 'B entry liquidity', CASE WHEN liq IS NULL THEN '9 unknown' WHEN liq < 10000 THEN '1 <$10k'
      WHEN liq < 50000 THEN '2 $10-50k' WHEN liq < 250000 THEN '3 $50-250k'
      WHEN liq < 1000000 THEN '4 $250k-1M' ELSE '5 >$1M' END, pnl_usd FROM c)
SELECT substr(factor, 3) AS factor, substr(bk, 3) AS bucket, count(*) AS n,
  ROUND(avg((pnl_usd > 0)::int) * 100) AS win_pct, ROUND(avg(pnl_usd)::numeric, 1) AS exp_usd,
  ROUND(sum(pnl_usd)::numeric, 0) AS net_usd
FROM b GROUP BY factor, bk ORDER BY factor, bk""")


def changes_before_after(s):  # noqa: C901
    fam = family(s, "t")
    base = "t.bot_id='copy' AND t.fill_status='closed' AND t.mode IN ('paper','archived')"
    return table("Did each change help? — 14 days before vs since (until next change)", f"""SELECT to_char(c.changed_at AT TIME ZONE '{TZ}', 'YYYY-MM-DD HH24:MI') AS changed, c.kind, c.description,
  b.n AS n_before, b.e AS exp_before_usd, a.n AS n_after, a.e AS exp_after_usd,
  ROUND(a.e - b.e, 1) AS delta_exp_usd
FROM strategy_changes c
LEFT JOIN LATERAL (SELECT count(*) n, ROUND(avg(t.pnl_usd)::numeric, 1) e FROM trades t
  WHERE {base} AND {fam} AND t.entry_at >= c.changed_at - interval '14 days' AND t.entry_at < c.changed_at) b ON true
LEFT JOIN LATERAL (SELECT count(*) n, ROUND(avg(t.pnl_usd)::numeric, 1) e FROM trades t
  WHERE {base} AND {fam} AND t.entry_at >= c.changed_at
    AND t.entry_at < COALESCE((SELECT min(c2.changed_at) FROM strategy_changes c2
                               WHERE c2.strategy IN ('{s}','fleet') AND c2.changed_at > c.changed_at), now())) a ON true
WHERE c.strategy IN ('{s}','fleet') ORDER BY c.changed_at DESC""",
        desc="Expectancy per trade for trades ENTERED in the 14 days before each logged change vs after it. "
             "Log changes with scripts/log_change.py.")


def closed_trades(strategies):
    return table("Closed trades (time range, latest 100)", f"""SELECT t.id, t.sim_metadata->>'strategy' AS strategy, t.asset,
  LEFT(COALESCE(t.sim_metadata->>'trigger_wallet', t.sim_metadata->>'team_id', ''), 10) AS trigger,
  ROUND(t.size_usd::numeric, 0) AS size_usd, ROUND(t.pnl_usd::numeric, 2) AS pnl_usd, ROUND(t.pnl_pct::numeric, 1) AS pnl_pct,
  t.exit_reason, ROUND((EXTRACT(EPOCH FROM t.exit_at - t.entry_at) / 3600)::numeric, 2) AS hold_h,
  ROUND((t.sim_metadata->>'token_age_at_entry_hours')::numeric, 2) AS token_age_h,
  ROUND((t.sim_metadata->>'entry_liquidity_usd')::numeric, 0) AS entry_liq_usd, t.entry_at, t.exit_at
FROM trades t WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='closed'
  AND t.sim_metadata->>'strategy' IN ({in_list(strategies)}) AND $__timeFilter(t.exit_at)
ORDER BY t.exit_at DESC LIMIT 100""")


def halt_history(bot_ids):
    return table("Halt history (time range)", f"""SELECT bot_id, halt_type, severity, halted_at, resumed_at, resumed_by, LEFT(reason, 120) AS reason
FROM halts WHERE bot_id IN ({in_list(bot_ids)}) AND $__timeFilter(halted_at) ORDER BY halted_at DESC""")


# ----------------------------------------------------------------------- modules ----
def m_wallet_trigger(s):
    return table("P&L by trigger wallet (time range) — the cull lever", f"""SELECT t.sim_metadata->>'trigger_wallet' AS wallet, COALESCE(wp.tier, '?') AS tier,
  COALESCE(ws.cls, '?') AS style, count(*) AS n, ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd,
  ROUND(avg(t.pnl_usd)::numeric, 1) AS exp_usd, ROUND(avg((t.pnl_usd > 0)::int) * 100) AS win_pct,
  CASE WHEN count(*) >= 5 AND sum(t.pnl_usd) < 0 THEN 'demote?' ELSE '' END AS flag
FROM trades t
LEFT JOIN wallet_pool wp ON wp.address = t.sim_metadata->>'trigger_wallet'
LEFT JOIN wallet_style ws ON ws.address = t.sim_metadata->>'trigger_wallet'
WHERE {closed(s, 't')} AND $__timeFilter(t.exit_at) AND t.sim_metadata->>'trigger_wallet' IS NOT NULL
GROUP BY 1, 2, 3 ORDER BY net_usd DESC""",
        desc="'demote?' = at least 5 trades and net negative. Advisory — you decide.")


def m_wallet_attrib():
    return table("P&L by wallet — attributed share, pnl / cluster size (time range)", f"""SELECT wa.wallet_address AS wallet, COALESCE(wp.tier, 'removed') AS tier, COALESCE(ws.cls, '?') AS style,
  count(*) AS n, ROUND(sum(wa.attributed_pnl_usd)::numeric, 0) AS net_usd,
  ROUND(avg(wa.attributed_pnl_usd)::numeric, 1) AS exp_usd,
  ROUND(avg((wa.attributed_pnl_usd > 0)::int) * 100) AS win_pct,
  CASE WHEN count(*) >= 5 AND sum(wa.attributed_pnl_usd) < 0 THEN 'demote?' ELSE '' END AS flag
FROM wallet_attributions wa JOIN trades t ON t.id = wa.trade_id
LEFT JOIN wallet_pool wp ON wp.address = wa.wallet_address
LEFT JOIN wallet_style ws ON ws.address = wa.wallet_address
WHERE wa.bot_id = 'copy' AND {closed('cluster', 't')} AND $__timeFilter(t.exit_at)
GROUP BY 1, 2, 3 ORDER BY net_usd DESC""",
        desc="Each trade's P&L split evenly across the wallets in its cluster. 'demote?' = >=5 trades and net negative.")


def m_team(s, label):
    track = "t.sim_metadata->>'strategy'" if s == "teamfollow" else f"'{s}'"
    strat_in = in_list(["teamfollow", "teamfollow_watch"]) if s == "teamfollow" else f"'{s}'"
    status = ("COALESCE(ts.status, 'active')" if s == "teamfollow" else "'-'")
    join = ("LEFT JOIN teamfollow_team_status ts ON ts.team_id::text = t.sim_metadata->>'team_id'"
            if s == "teamfollow" else "")
    return table(f"P&L by {label} (time range)", f"""SELECT t.sim_metadata->>'team_id' AS {label}, {status} AS team_status, {track} AS track,
  count(*) AS n, ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd, ROUND(avg(t.pnl_usd)::numeric, 1) AS exp_usd,
  ROUND(avg((t.pnl_usd > 0)::int) * 100) AS win_pct
FROM trades t {join}
WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='closed' AND t.sim_metadata->>'strategy' IN ({strat_in})
  AND t.sim_metadata->>'team_id' IS NOT NULL AND $__timeFilter(t.exit_at)
GROUP BY 1, 2, 3 ORDER BY net_usd DESC""")


def m_team_lifecycle():
    return table("Team lifecycle — promoted (active) vs watch track (time range)", """SELECT t.sim_metadata->>'strategy' AS track, count(DISTINCT t.sim_metadata->>'team_id') AS teams, count(*) AS n,
  ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd, ROUND(avg(t.pnl_usd)::numeric, 1) AS exp_usd,
  ROUND(avg((t.pnl_usd > 0)::int) * 100) AS win_pct
FROM trades t WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='closed'
  AND t.sim_metadata->>'strategy' IN ('teamfollow','teamfollow_watch') AND $__timeFilter(t.exit_at)
GROUP BY 1 ORDER BY 1""",
        desc="Watch teams trade on paper but are excluded from the scorecard until promoted.")


def m_promo_source():
    return table("P&L by promo source (time range)", f"""SELECT sf.features_json->>'source' AS source, count(*) AS n, ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd,
  ROUND(avg(t.pnl_usd)::numeric, 1) AS exp_usd, ROUND(avg((t.pnl_usd > 0)::int) * 100) AS win_pct
FROM trades t JOIN signal_features sf ON sf.trade_id = t.id
WHERE {closed('promobuy', 't')} AND sf.features_json->>'source' IS NOT NULL AND $__timeFilter(t.exit_at)
GROUP BY 1 ORDER BY net_usd DESC""")


def m_promo_track():
    return table("Per-track performance — has-liq vs null-liq (realized incl. open partials, era)", """WITH r AS (SELECT (CASE WHEN sim_metadata->>'entry_liquidity_usd' IS NULL THEN 'null-liq (small)' ELSE 'has-liq (full)' END) AS track,
  fill_status, pnl_usd, size_usd,
  (SELECT COALESCE(SUM((e->>'received_usdc')::numeric - size_usd * (e->>'fraction')::numeric), 0)
   FROM json_array_elements(sim_metadata->'partial_exits') e WHERE e->>'status' = 'filled') AS open_part
  FROM trades WHERE bot_id='copy' AND mode='paper' AND sim_metadata->>'strategy'='promobuy')
SELECT track, COUNT(*) FILTER (WHERE fill_status='closed') AS closed, COUNT(*) FILTER (WHERE fill_status='open') AS riding,
  ROUND((COUNT(*) FILTER (WHERE fill_status='closed' AND pnl_usd > 0)::float
         / NULLIF(COUNT(*) FILTER (WHERE fill_status='closed'), 0) * 100)::numeric, 1) AS win_pct,
  ROUND((COALESCE(SUM(pnl_usd) FILTER (WHERE fill_status='closed'), 0)
         + COALESCE(SUM(open_part) FILTER (WHERE fill_status='open'), 0))::numeric, 0) AS net_realized_usd,
  ROUND(SUM(size_usd)::numeric, 0) AS capital_usd
FROM r GROUP BY 1 ORDER BY 1""")


def m_bucketed(title, s, expr, buckets_case):
    return table(title, f"""SELECT substr(b, 3) AS bucket, count(*) AS n, ROUND(avg((pnl_usd > 0)::int) * 100) AS win_pct,
  ROUND(avg(pnl_usd)::numeric, 1) AS exp_usd, ROUND(sum(pnl_usd)::numeric, 0) AS net_usd
FROM (SELECT CASE {buckets_case} END AS b, pnl_usd FROM (SELECT {expr} AS v, pnl_usd FROM trades
      WHERE {closed(s)} AND $__timeFilter(exit_at)) x) y
GROUP BY b ORDER BY b""")


def m_cluster_size():
    return m_bucketed("Outcome by cluster size (time range)", "cluster",
                      "(sim_metadata->>'cluster_size')::int",
                      "WHEN v IS NULL THEN '9 ?' WHEN v <= 3 THEN '1 3' WHEN v <= 5 THEN '2 4-5' "
                      "WHEN v <= 8 THEN '3 6-8' ELSE '4 9+'")


def m_nbuys():
    return m_bucketed("Outcome by buys at entry (time range)", "conviction",
                      "(sim_metadata->>'conviction_n_buys')::int",
                      "WHEN v IS NULL THEN '9 ?' WHEN v = 1 THEN '1 1' WHEN v = 2 THEN '2 2' "
                      "WHEN v <= 5 THEN '3 3-5' WHEN v <= 10 THEN '4 6-10' ELSE '5 >10'")


def m_hold():
    return m_bucketed("Hold time — thesis is multi-day (time range)", "swing",
                      "EXTRACT(EPOCH FROM exit_at - entry_at) / 3600",
                      "WHEN v < 1.0/6 THEN '1 <10m' WHEN v < 1 THEN '2 10m-1h' WHEN v < 6 THEN '3 1-6h' "
                      "WHEN v < 24 THEN '4 6-24h' WHEN v < 72 THEN '5 1-3d' ELSE '6 3-10d'")


def m_style(s):
    return table("P&L by wallet style — sniper / MM / selector (time range)", f"""SELECT COALESCE(ws.cls, 'UNCLASSIFIED') AS style, count(DISTINCT t.id) AS trades,
  ROUND(sum(t.pnl_usd / json_array_length(t.sim_metadata->'cluster_wallets'))::numeric, 0) AS attributed_net_usd,
  ROUND((sum(t.pnl_usd / json_array_length(t.sim_metadata->'cluster_wallets')) / NULLIF(count(DISTINCT t.id), 0))::numeric, 1) AS exp_usd
FROM trades t CROSS JOIN LATERAL json_array_elements_text(t.sim_metadata->'cluster_wallets') w(addr)
LEFT JOIN wallet_style ws ON ws.address = w.addr
WHERE {closed(s, 't')} AND json_typeof(t.sim_metadata->'cluster_wallets') = 'array'
  AND json_array_length(t.sim_metadata->'cluster_wallets') > 0 AND $__timeFilter(t.exit_at)
GROUP BY 1 ORDER BY attributed_net_usd""",
        desc="Each trade's P&L split across its participating wallets, grouped by the wallet's style class "
             "(scripts/wallet_style_classify.py). Snipers/MMs are structurally unfollowable.")


def b_trigger_wallet(s):
    return bar("Net P&L per trigger wallet — (n) = trades (time range)", f"""SELECT LEFT(t.sim_metadata->>'trigger_wallet', 6) || ' (' || count(*) || ')' AS label,
  ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd
FROM trades t WHERE {closed(s, 't')} AND $__timeFilter(t.exit_at) AND t.sim_metadata->>'trigger_wallet' IS NOT NULL
GROUP BY t.sim_metadata->>'trigger_wallet' ORDER BY net_usd DESC""")


def b_wallet_attrib():
    return bar("Net P&L per wallet — best & worst 15, (n) = trades (attributed share, time range)", f"""WITH w AS (
  SELECT wa.wallet_address, count(*) AS n, sum(wa.attributed_pnl_usd) AS net
  FROM wallet_attributions wa JOIN trades t ON t.id = wa.trade_id
  WHERE wa.bot_id = 'copy' AND {closed('cluster', 't')} AND $__timeFilter(t.exit_at)
  GROUP BY 1),
r AS (SELECT *, row_number() OVER (ORDER BY net DESC) AS top, row_number() OVER (ORDER BY net ASC) AS bot FROM w)
SELECT LEFT(wallet_address, 6) || ' (' || n || ')' AS label, ROUND(net::numeric, 0) AS net_usd
FROM r WHERE top <= 15 OR bot <= 15 ORDER BY net DESC""",
        desc="All wallets are in the table below; the chart shows the 15 best and 15 worst.")


def b_team(s, prefix):
    strat_in = in_list(["teamfollow", "teamfollow_watch"]) if s == "teamfollow" else f"'{s}'"
    watch = ("|| CASE WHEN bool_or(t.sim_metadata->>'strategy' = 'teamfollow_watch') "
             "AND NOT bool_or(t.sim_metadata->>'strategy' = 'teamfollow') THEN ' w' ELSE '' END"
             if s == "teamfollow" else "")
    return bar(f"Net P&L per {'team' if prefix == 'T' else 'cohort'} — (n) = trades"
               + (", w = watch track" if s == "teamfollow" else "") + " (time range)",
               f"""SELECT '{prefix}' || (t.sim_metadata->>'team_id') {watch} || ' (' || count(*) || ')' AS label,
  ROUND(sum(t.pnl_usd)::numeric, 0) AS net_usd
FROM trades t WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='closed'
  AND t.sim_metadata->>'strategy' IN ({strat_in}) AND t.sim_metadata->>'team_id' IS NOT NULL
  AND $__timeFilter(t.exit_at)
GROUP BY t.sim_metadata->>'team_id' ORDER BY net_usd DESC""")


BARS = {  # strategies with wallets or teams get the per-entity bar chart (Roy 2026-09-25)
    "cluster": b_wallet_attrib,
    "conviction": lambda: b_trigger_wallet("conviction"),
    "swing": lambda: b_trigger_wallet("swing"),
    "teamfollow": lambda: b_team("teamfollow", "T"),
    "cohortfire": lambda: b_team("cohortfire", "C"),
}


MODULES = {
    "cluster":    [m_wallet_attrib, lambda: m_style("cluster"), m_cluster_size],
    "conviction": [lambda: m_wallet_trigger("conviction"), lambda: m_style("conviction"), m_nbuys],
    "swing":      [lambda: m_wallet_trigger("swing"), lambda: m_style("swing"), m_hold],
    "teamfollow": [lambda: m_team("teamfollow", "team"), m_team_lifecycle, lambda: m_style("teamfollow")],
    "cohortfire": [lambda: m_team("cohortfire", "cohort")],
    "promobuy":   [m_promo_source, m_promo_track],
}

PAGES = {  # strategy -> (uid, title, blurb)
    "cluster":    ("copy-detail", "COPY · Cluster", "3+ active wallets co-buy within 15 min → enter; own ladder/trailing exits."),
    "conviction": ("copy-conviction", "COPY · Conviction", "Single-wallet accumulation trigger. HALTED manually 2026-08-05 (entry gate being redesigned)."),
    "swing":      ("copy-swing", "COPY · Swing", "Multi-day follow-in, exit when the trigger wallet net-distributes ≥50%."),
    "teamfollow": ("copy-teamfollow", "COPY · Team-follow", "≥2 members of a known team co-buy → enter. Watch-track teams trade on paper until promoted."),
    "cohortfire": ("copy-cohortfire", "COPY · Cohort-fire", "Red-cohort group co-buy with a $50k liquidity floor. Dormant by design."),
    "promobuy":   ("copy-promobuy", "COPY · Promo-buy", "Paid-promo tokens (Dexscreener) → enter; has-liq / null-liq tracks."),
}

LEGEND = ("*Stat strip = current era (since the strategy's last reset). Charts and tables follow the time "
          "picker. Blue markers = logged changes, red = halts. Open positions with current liquidity < $100 "
          "are marked **RUG at −100%**.*")


def strategy_page(s):
    _id[0] = 0
    uid, title, blurb = PAGES[s]
    open_strats = ["teamfollow", "teamfollow_watch"] if s == "teamfollow" else [s]
    mods = [m() for m in MODULES[s]]
    mod_rows = [[(mods[i], 12, 9)] + ([(mods[i + 1], 12, 9)] if i + 1 < len(mods) else [])
                for i in range(0, len(mods), 2)]
    if len(mod_rows[-1]) == 1:
        mod_rows[-1] = [(mod_rows[-1][0][0], 24, 9)]
    rows = [
        [(text_panel(f"## {title}\n{blurb}  \n{LEGEND}"), 24, 3)],
        stat_strip(s),
        [(rolling_expectancy([s]), 12, 9), (pnl_distribution(s), 12, 9)],
        [(exit_reasons(s), 12, 13), (entry_conditions(s), 12, 13)],
        [(changes_before_after(s), 24, 6)],
        *([[(BARS[s](), 24, 9)]] if s in BARS else []),
        *mod_rows,
        [(table("Open positions — current liquidity, rug-marked", open_positions_sql(open_strats)), 24, 11)],
        [(closed_trades(open_strats), 24, 10)],
        [(halt_history([halt_id(s)]), 24, 6)],
    ]
    return dashboard(uid, title, layout(rows), ["copy", s], strategies=[s],
                     time_from=DEFAULT_RANGE.get(s, "now-30d"))


# --------------------------------------------------------------- Fleet Command -----
def attention_sql():
    strat_rows = ",".join(f"('{s}','{halt_id(s)}',{'false' if s in DORMANT_BY_DESIGN else 'true'})" for s in STRATS)
    return f"""WITH strat(strategy, halt_id, expect_active) AS (VALUES {strat_rows}),
{open_cte()},
items AS (
  SELECT CASE WHEN h.halt_type = 'manual' THEN 'INFO' WHEN h.severity = 'p0' OR h.halt_type = 'dd_total' THEN 'P0' ELSE 'P1' END AS sev,
    'halt' AS kind, h.bot_id AS subject,
    h.halt_type || ' — halted ' || ROUND((EXTRACT(EPOCH FROM now() - h.halted_at) / 86400)::numeric, 1) || 'd ago'
      || CASE WHEN h.halt_type = 'dd_total' THEN ' (does not auto-resume: needs your decision)' ELSE '' END AS detail
  FROM halts h WHERE h.resumed_at IS NULL
  UNION ALL
  SELECT 'P2', 'idle', s.strategy, 'running but no entry for '
         || ROUND((EXTRACT(EPOCH FROM now() - max(t.entry_at)) / 3600)::numeric, 0) || 'h'
  FROM strat s JOIN trades t ON t.bot_id = 'copy' AND t.mode = 'paper' AND t.sim_metadata->>'strategy' = s.strategy
  WHERE s.expect_active AND NOT EXISTS (SELECT 1 FROM halts h WHERE h.bot_id = s.halt_id AND h.resumed_at IS NULL)
  GROUP BY s.strategy HAVING max(t.entry_at) < now() - interval '24 hours'
  UNION ALL
  SELECT 'P0', 'heartbeat', process_name, 'no heartbeat for ' || EXTRACT(EPOCH FROM now() - last_ping_at)::int || 's'
  FROM heartbeats WHERE last_ping_at < now() - interval '5 minutes'
  UNION ALL
  SELECT 'P1', 'rug / liquidity', strategy, count(*) || ' open position(s) ' || string_agg(DISTINCT status, '/')
         || ' — $' || ROUND(sum(rem)) || ' exposed'
  FROM o WHERE status IN ('RUG', 'LIQ COLLAPSED') GROUP BY strategy
  UNION ALL
  SELECT 'P2', 'liquidity data', strategy, count(*) || ' open position(s) with no liquidity reading in 2h'
  FROM o WHERE status IN ('STALE', 'NO DATA') GROUP BY strategy
  UNION ALL
  SELECT 'P1', 'webhooks', 'wallet swaps', 'last hour ' || n1 || ' swaps vs ' || ROUND(avg7) || '/h (7d avg)'
  FROM (SELECT (SELECT count(*) FROM wallet_swaps_log WHERE event_at > now() - interval '1 hour') AS n1,
               (SELECT count(*) / 168.0 FROM wallet_swaps_log WHERE event_at > now() - interval '7 days') AS avg7) w
  WHERE w.n1 < 0.3 * w.avg7)
SELECT sev, kind, subject, detail FROM (
  SELECT * FROM items
  UNION ALL SELECT 'OK', 'all clear', 'fleet', 'nothing needs attention' WHERE NOT EXISTS (SELECT 1 FROM items)) x
ORDER BY CASE sev WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'INFO' THEN 3 ELSE 4 END, subject"""


def scorecard_sql():
    vals = ",".join(f"('{s}','{halt_id(s)}')" for s in STRATS)
    return f"""WITH strat(strategy, halt_id) AS (VALUES {vals}),
c AS (SELECT sim_metadata->>'strategy' AS st, pnl_usd, pnl_pct, exit_at,
        row_number() OVER (PARTITION BY sim_metadata->>'strategy' ORDER BY pnl_usd DESC) AS rk,
        count(*) OVER (PARTITION BY sim_metadata->>'strategy') AS cnt,
        row_number() OVER (PARTITION BY sim_metadata->>'strategy' ORDER BY exit_at DESC) AS recent
      FROM trades WHERE bot_id='copy' AND mode='paper' AND fill_status='closed'
        AND sim_metadata->>'strategy' IN ({in_list(STRATS)})),
agg AS (SELECT st, count(*) AS n, avg(pnl_usd) AS exp_usd, avg(pnl_pct) AS exp_pct, avg((pnl_usd > 0)::int) * 100 AS win,
          avg(pnl_usd) FILTER (WHERE pnl_usd > 0) AS aw, avg(pnl_usd) FILTER (WHERE pnl_usd <= 0) AS al,
          sum(pnl_usd) AS net, sum(pnl_usd) FILTER (WHERE rk > ceil(cnt * 0.05)) AS net_ex5,
          sum(pnl_usd) FILTER (WHERE exit_at > now() - interval '7 days') AS net7,
          avg(pnl_usd) FILTER (WHERE recent <= 50) AS exp50
        FROM c GROUP BY st),
{open_cte(STRATS)},
oa AS (SELECT strategy AS st, count(*) AS n_open, sum(unreal) AS unreal, count(*) FILTER (WHERE status = 'RUG') AS rugs
       FROM ou GROUP BY strategy),
e AS (SELECT sim_metadata->>'strategy' AS st, min(entry_at) AS era_start, max(entry_at) AS last_entry
      FROM trades WHERE bot_id='copy' AND mode='paper' AND sim_metadata->>'strategy' IN ({in_list(STRATS)}) GROUP BY 1)
SELECT s.strategy,
  COALESCE('HALTED · ' || h.halt_type, 'running') AS state,
  to_char(e.era_start AT TIME ZONE '{TZ}', 'YYYY-MM-DD') AS era_start,
  COALESCE(a.n, 0) AS n_closed,
  ROUND(a.exp_usd::numeric, 1) AS exp_usd, ROUND(a.win::numeric, 0) AS win_pct,
  ROUND((a.aw / NULLIF(abs(a.al), 0))::numeric, 2) AS payoff,
  ROUND(a.net::numeric, 0) AS net_usd, ROUND(a.net_ex5::numeric, 0) AS ex_top5_usd,
  ROUND(COALESCE(a.net7, 0)::numeric, 0) AS net_7d_usd,
  COALESCE(oa.n_open, 0) AS open_n, ROUND(COALESCE(oa.unreal, 0), 0) AS open_usd,
  ROUND((COALESCE(a.net, 0) + COALESCE(oa.unreal, 0))::numeric, 0) AS all_in_usd,
  ROUND((EXTRACT(EPOCH FROM now() - e.last_entry) / 3600)::numeric, 0) AS last_entry_h,
  concat_ws(' · ',
    CASE WHEN a.exp50 < 0 THEN 'exp<0 last 50' END,
    CASE WHEN h.halt_type IS NULL AND e.last_entry < now() - interval '24 hours' THEN 'no entry 24h' END,
    CASE WHEN oa.rugs > 0 THEN oa.rugs || ' rug open' END,
    CASE WHEN COALESCE(a.n, 0) < 30 THEN 'low N' END,
    CASE WHEN a.net > 0 AND a.net_ex5 < 0 THEN 'tail-dependent' END) AS flags
FROM strat s
LEFT JOIN agg a ON a.st = s.strategy
LEFT JOIN oa ON oa.st = s.strategy
LEFT JOIN e ON e.st = s.strategy
LEFT JOIN LATERAL (SELECT halt_type FROM halts WHERE bot_id = s.halt_id AND resumed_at IS NULL
                   ORDER BY halted_at DESC LIMIT 1) h ON true
ORDER BY CASE s.strategy {' '.join(f"WHEN '{x}' THEN {i}" for i, x in enumerate(STRATS))} END"""


def fleet_command():
    _id[0] = 0
    all_open = STRATS + ["teamfollow_watch"]
    rows = [
        [(text_panel("## Fleet Command\nWhat needs you first, then how each strategy is doing *per trade* in its "
                     "current era. Watch-track teams and retired (_pre_reset) eras are excluded from the scorecard by "
                     "design.  \n" + LEGEND), 24, 3)],
        [(table("Needs your attention", attention_sql(),
                desc="P0 = act now, P1 = look today, P2 = when convenient, INFO = deliberate state. "
                     "Empty of problems = 'all clear'.",
                widths={"kind": 150, "subject": 170}), 24, 7)],
        [(table("Scorecard — per strategy, current era", scorecard_sql(),
                desc="exp = average per closed trade. payoff = avg win / avg loss. net = realized in the current era. "
                     "ex_top5 = net without the best 5% of trades (tail dependence). open = unrealized on open positions; "
                     "open and all_in mark rugs (liq < $100) at -100%. "
                     "Flags are advisory.",
                widths={"strategy": 100, "state": 150, "era_start": 100, "flags": 270}), 24, SCORECARD_H)],
        [(rolling_expectancy(STRATS), 24, 9)],
        [(table("Open positions — all strategies, current liquidity, rug-marked", open_positions_sql(all_open)), 24, 12)],
        [(closed_trades(all_open), 24, 10)],
    ]
    return dashboard("fleet-overview", "Fleet Command", layout(rows), ["fleet"])


# ---------------------------------------------------------------- Ops & Health -----
def ops_health(service_usage_src):
    _id[0] = 0
    rows = [
        [(text_panel("## Ops & Health\nPlumbing, fills, and API budgets. (Helius/Birdeye panels at the bottom.)"), 24, 2)],
        [(table("Heartbeats", "SELECT process_name, last_ping_at, EXTRACT(EPOCH FROM now() - last_ping_at)::int AS secs_ago, "
                              "CASE WHEN last_ping_at >= now() - interval '2 minutes' THEN 'ok' ELSE 'STALE' END AS status "
                              "FROM heartbeats ORDER BY process_name"), 8, 7),
         (timeseries("Wallet swaps delivered per hour (webhooks)",
                     "SELECT date_trunc('hour', event_at) AS time, source_webhook AS metric, count(*) AS value "
                     "FROM wallet_swaps_log WHERE $__timeFilter(event_at) GROUP BY 1, 2 ORDER BY 1",
                     unit="short", zero_line=False), 16, 7)],
        [(timeseries("No-fill rate per day by strategy (%)",
                     f"SELECT date_trunc('day', created_at) AS time, sim_metadata->>'strategy' AS metric, "
                     f"ROUND(100.0 * count(*) FILTER (WHERE fill_status = 'no_fill') / count(*), 1) AS value "
                     f"FROM trades WHERE bot_id='copy' AND mode='paper' AND $__timeFilter(created_at) "
                     f"AND sim_metadata->>'strategy' IN ({in_list(STRATS)}) GROUP BY 1, 2 ORDER BY 1",
                     unit="percent", zero_line=False), 12, 8),
         (table("Slippage by strategy (time range)", f"""SELECT sim_metadata->>'strategy' AS strategy, count(*) AS n,
  ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY (sim_metadata->>'slippage_bps')::float)::numeric, 0) AS entry_med_bps,
  ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY (sim_metadata->>'slippage_bps')::float)::numeric, 0) AS entry_p90_bps,
  ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY (sim_metadata->>'exit_slippage_bps')::float)::numeric, 0) AS exit_med_bps
FROM trades WHERE bot_id='copy' AND mode='paper' AND sim_metadata->>'slippage_bps' IS NOT NULL
  AND sim_metadata->>'strategy' IN ({in_list(STRATS)}) AND $__timeFilter(created_at)
GROUP BY 1 ORDER BY 1"""), 12, 8)],
        [(halt_history([halt_id(s) for s in STRATS]), 24, 6)],
    ]
    panels = layout(rows)
    y0 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    for p in service_usage_src.get("panels", []):
        q = json.loads(json.dumps(p))
        if q.get("type") == "text":
            continue
        q["id"] = pid()
        q["gridPos"] = dict(q["gridPos"], y=q["gridPos"]["y"] + y0)
        panels.append(q)
    return dashboard("ops-health", "Ops & Health", panels, ["ops"], time_from="now-7d")


# ----------------------------------------------------------------- Wallet Pool -----
def wallet_pool():
    _id[0] = 0
    classes = ["SELECTOR", "SNIPER", "MM_HFT", "MM_ESTABLISHED", "OTHER"]
    cols = ", ".join(f"count(*) FILTER (WHERE ws.cls = '{c}') AS \"{c}\"" for c in classes)
    rows = [
        [(text_panel("## Wallet Pool\nWho we follow. Style = median token age at the wallet's entry on its winners: "
                     "SNIPER (launch, unfollowable) · MM (market-maker, unfollowable) · SELECTOR (hours-old, the "
                     "copyable edge). Re-tier 2026-09-25 pruned 174 snipers/MMs from active."), 24, 3)],
        [(table("Pool by tier × wallet style", f"""SELECT wp.tier, count(*) AS wallets, {cols},
  count(*) FILTER (WHERE ws.cls IS NULL) AS unclassified,
  count(*) FILTER (WHERE wp.swing) AS swing_roster, count(*) FILTER (WHERE wp.conviction) AS conviction_roster,
  count(*) FILTER (WHERE wp.pinned) AS pinned
FROM wallet_pool wp LEFT JOIN wallet_style ws ON ws.address = wp.address
GROUP BY wp.tier ORDER BY CASE wp.tier WHEN 'active' THEN 1 WHEN 'teamfollow' THEN 2 WHEN 'watch' THEN 3 ELSE 4 END"""), 24, 6)],
        [(timeseries("Daily wallet pool transitions",
                     "SELECT date_trunc('day', created_at) AS time, "
                     "COALESCE(jsonb_array_length((payload->>'promoted')::jsonb), 0) AS promoted, "
                     "COALESCE(jsonb_array_length((payload->>'demoted')::jsonb), 0) AS demoted, "
                     "COALESCE(jsonb_array_length((payload->>'dropped')::jsonb), 0) AS dropped, "
                     "COALESCE(jsonb_array_length((payload->>'swapped')::jsonb), 0) AS swapped "
                     "FROM audit_log WHERE event_type = 'wallet_pool_daily_transitions' "
                     "AND $__timeFilter(created_at) ORDER BY 1", unit="short", zero_line=False), 12, 8),
         (table("Unfollowable wallets still active (protected: pinned / swing / conviction)",
                """SELECT wp.address AS wallet, ws.cls AS style, ROUND(ws.median_age_h::numeric, 2) AS median_age_h,
  wp.pinned, wp.swing, wp.conviction, wp.source
FROM wallet_pool wp JOIN wallet_style ws ON ws.address = wp.address
WHERE wp.tier = 'active' AND ws.cls IN ('SNIPER', 'MM_HFT', 'MM_ESTABLISHED') ORDER BY ws.cls, wp.address""",
                desc="The re-tier skipped these on purpose. Review whether swing/conviction should keep them."), 12, 8)],
        [(table("Active SELECTOR wallets — the copyable cohort", """SELECT wp.address AS wallet, ROUND(ws.median_age_h::numeric, 2) AS median_age_h, ws.swaps, ws.tokens,
  wp.swing, wp.conviction, wp.pinned, wp.events_30d, wp.source
FROM wallet_pool wp JOIN wallet_style ws ON ws.address = wp.address
WHERE wp.tier = 'active' AND ws.cls = 'SELECTOR' ORDER BY ws.median_age_h"""), 12, 10),
         (table("Watch-tier promotion candidates", """WITH ev AS (SELECT wallet_address, COUNT(*) FILTER (WHERE event_at >= now() - interval '7 days') AS n7d,
  COUNT(*) AS n30d FROM wallet_events_log WHERE event_at >= now() - interval '30 days' GROUP BY 1)
SELECT wp.address AS wallet, COALESCE(ws.cls, '?') AS style, COALESCE(e.n7d, 0) AS events_7d, COALESCE(e.n30d, 0) AS events_30d,
  wp.last_event_at, wp.source
FROM wallet_pool wp LEFT JOIN ev e ON e.wallet_address = wp.address LEFT JOIN wallet_style ws ON ws.address = wp.address
WHERE wp.tier = 'watch' ORDER BY COALESCE(e.n7d, 0) DESC, COALESCE(e.n30d, 0) DESC LIMIT 25"""), 12, 10)],
    ]
    return dashboard("wallet-pool", "Wallet Pool", layout(rows), ["pool"], time_from="now-30d")


# ------------------------------------------------------------------------- main -----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="monitoring/dashboards")
    ap.add_argument("--service-usage-src", default="scripts/dashboard_src/service-usage.src.json")
    a = ap.parse_args()
    su = json.load(open(a.service_usage_src, encoding="utf-8"))
    files = {"fleet-overview.json": fleet_command(), "ops-health.json": ops_health(su),
             "wallet-pool.json": wallet_pool()}
    fname = {"cluster": "copy-detail.json"}
    for s in STRATS:
        files[fname.get(s, f"copy-{s}.json")] = strategy_page(s)
    os.makedirs(a.out, exist_ok=True)
    for f, d in files.items():
        with open(os.path.join(a.out, f), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh, indent=1, ensure_ascii=False)
        print(f"wrote {f:22} panels={len(d['panels'])}")


if __name__ == "__main__":
    main()
