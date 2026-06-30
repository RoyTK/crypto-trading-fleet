"""COPY daily digest — one phone-glanceable Discord message per day.

Read-only: queries the last 24h of COPY state and posts a single P2 Discord
message (no ping). Touches nothing — no trading logic, no money, fully
isolated from the bot. Worst case of a bug is a missing/ugly message.

Built 2026-06-12 for Roy's 9-day vacation: he can glance at Discord on his
phone once a day and know COPY is alive and behaving, without monitoring.
The real safety nets (dd_monitor auto-halt, watchdog) run independently;
this is awareness, not control.

Posts as P2 (Discord, no ping) rather than P3 (collected digest) because P3
is "collected, not pushed" and we want guaranteed same-message delivery to
the phone.

Run manually to test (posts immediately):
  docker compose exec -T framework python -m scripts.daily_digest

Cron (daily 13:00 UTC = 08:00 CT, ready when he wakes):
  0 13 * * * cd ~/crypto-fleet && docker compose exec -T framework python -m scripts.daily_digest >> ~/logs/daily_digest.log 2>&1
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from bots.copy.config import get_copy_settings
from framework.alerts import emit_alert
from framework.db import session_scope
from framework.logging_setup import get_logger
from monitoring.alerting.taxonomy import Severity


log = get_logger("daily_digest")

# Option A (exit clusters fire on every wave) deployed ~2026-06-10 21:13 UTC.
# Forward check: the first sell_cluster close after this is the execution-
# level proof Option A works in production.
OPTION_A_DEPLOY = "2026-06-10 21:13:00+00"


def _scalar(s, sql: str, default=None, **params):
    try:
        v = s.execute(text(sql), params).scalar()
        return v if v is not None else default
    except Exception:
        return default


def _build() -> str:
    paper_cap = float(get_copy_settings().copy_paper_capital_usd or 10000.0)
    lines: list[str] = []

    with session_scope() as s:
        # 24h closed paper trades
        row = s.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(ROUND(SUM(pnl_usd)::numeric, 2), 0) AS pnl
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND fill_status='closed'
              AND exit_at >= NOW() - INTERVAL '24 hours'
              AND (sim_metadata->>'strategy') = 'cluster'
        """)).first()
        n24 = int(row.n or 0)
        wins24 = int(row.wins or 0)
        pnl24 = float(row.pnl or 0.0)
        wr24 = (wins24 / n24 * 100.0) if n24 else 0.0

        # open positions + true remaining exposure
        orow = s.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(ROUND(SUM(COALESCE((sim_metadata->>'remaining_size_usd')::numeric, size_usd))::numeric, 0), 0) AS alloc
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND fill_status='open'
              AND (sim_metadata->>'strategy') = 'cluster'
        """)).first()
        open_n = int(orow.n or 0)
        alloc = float(orow.alloc or 0.0)
        alloc_pct = (alloc / paper_cap * 100.0) if paper_cap else 0.0

        # halts 24h
        hrow = s.execute(text("""
            SELECT COUNT(*) AS n, COALESCE(STRING_AGG(DISTINCT halt_type, ','), '') AS types
            FROM halts WHERE halted_at >= NOW() - INTERVAL '24 hours'
        """)).first()
        halt_n = int(hrow.n or 0)
        halt_types = hrow.types or ""

        # promotions 24h
        promo_n = _scalar(s, """
            SELECT COUNT(*) FROM wallet_pool WHERE promoted_at >= NOW() - INTERVAL '24 hours'
        """, 0)

        # pool tiers
        active = _scalar(s, "SELECT COUNT(*) FROM wallet_pool WHERE tier='active'", 0)
        watch = _scalar(s, "SELECT COUNT(*) FROM wallet_pool WHERE tier='watch'", 0)

        # kill_criteria snapshot
        kc = s.execute(text("""
            SELECT kill_criteria_status->>'n' AS n,
                   kill_criteria_status->>'wr' AS wr,
                   kill_criteria_status->>'net_pnl_pct' AS net,
                   kill_criteria_status->>'sharpe' AS sharpe,
                   kill_criteria_status->>'kill_triggers' AS kt,
                   kill_criteria_status->>'warning_triggers' AS wt
            FROM bot_state WHERE bot_id='copy'
        """)).first()

        # ---- conviction sub-strategy (single-wallet trigger) ----
        conv_cap = float(get_copy_settings().copy_conviction_paper_capital_usd or 10000.0)
        conv_enabled = bool(get_copy_settings().copy_conviction_enabled)
        crow = s.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(ROUND(SUM(pnl_usd)::numeric, 2), 0) AS pnl
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND fill_status='closed'
              AND exit_at >= NOW() - INTERVAL '24 hours'
              AND (sim_metadata->>'strategy') = 'conviction'
        """)).first()
        conv_n24 = int(crow.n or 0)
        conv_wins24 = int(crow.wins or 0)
        conv_pnl24 = float(crow.pnl or 0.0)
        corow = s.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(ROUND(SUM(COALESCE((sim_metadata->>'remaining_size_usd')::numeric, size_usd))::numeric, 0), 0) AS alloc
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND fill_status='open'
              AND (sim_metadata->>'strategy') = 'conviction'
        """)).first()
        conv_open_n = int(corow.n or 0)
        conv_alloc = float(corow.alloc or 0.0)
        conv_total = int(_scalar(s,
            "SELECT COUNT(*) FROM trades WHERE bot_id='copy' "
            "AND (sim_metadata->>'strategy')='conviction'", 0) or 0)
        conv_roster = int(_scalar(s,
            "SELECT COUNT(*) FROM wallet_pool WHERE conviction = true", 0) or 0)
        ckc = s.execute(text("""
            SELECT kill_criteria_status->>'n' AS n,
                   kill_criteria_status->>'wr' AS wr,
                   kill_criteria_status->>'net_pnl_pct' AS net,
                   kill_criteria_status->>'sharpe' AS sharpe,
                   kill_criteria_status->>'kill_triggers' AS kt
            FROM bot_state WHERE bot_id='copy_conviction'
        """)).first()

        # sell-cluster closes since Option A (forward validation)
        sc = s.execute(text("""
            SELECT COUNT(*) AS n,
                   MIN(exit_at) AT TIME ZONE 'America/Chicago' AS first_ct
            FROM trades
            WHERE bot_id='copy' AND exit_reason='sell_cluster'
              AND exit_at >= :since
        """), {"since": OPTION_A_DEPLOY}).first()
        scc_n = int(sc.n or 0)

        # liveness — any stale heartbeat?
        stale = [r.process_name for r in s.execute(text("""
            SELECT process_name FROM heartbeats
            WHERE last_ping_at < NOW() - INTERVAL '10 minutes'
        """)).all()]

    # ---- compose ----
    lines.append(f"24h: {n24} trades, {wins24}W ({wr24:.0f}% WR), PnL {pnl24:+.2f}")
    lines.append(f"Open: {open_n} positions, ${alloc:.0f} ({alloc_pct:.1f}% of paper)")
    if halt_n:
        lines.append(f"⚠ Halts 24h: {halt_n} ({halt_types})")
    else:
        lines.append("Halts 24h: 0")
    lines.append(f"Pool: {active} active / {watch} watch  (+{promo_n} promoted 24h)")
    if kc and kc.n is not None:
        kt = kc.kt or "[]"
        wt = kc.wt or "[]"
        trig = "none" if kt in ("[]", "", None) else kt
        warn = "" if wt in ("[]", "", None) else f"  warn={wt}"
        sharpe = kc.sharpe if kc.sharpe not in (None, "") else "n/a"
        lines.append(f"Window: n={kc.n}, WR={float(kc.wr)*100:.1f}%, "
                     f"NetPnL={kc.net}%, Sharpe={sharpe}")
        lines.append(f"Triggers: {trig}{warn}")
    if scc_n:
        lines.append(f"✅ sell-cluster closes (Option A live): {scc_n}  first {sc.first_ct}")
    else:
        lines.append("sell-cluster closes since Option A: 0 (not yet observed)")
    if stale:
        lines.append(f"🔴 STALE heartbeats: {', '.join(stale)}")
    else:
        lines.append("Liveness: all processes fresh ✅")

    # ---- conviction section (only when enabled or it has ever traded) ----
    if conv_enabled or conv_total:
        conv_wr = (conv_wins24 / conv_n24 * 100.0) if conv_n24 else 0.0
        conv_alloc_pct = (conv_alloc / conv_cap * 100.0) if conv_cap else 0.0
        lines.append("")
        lines.append("— Conviction (single-wallet) —")
        lines.append(f"24h: {conv_n24} trades, {conv_wins24}W ({conv_wr:.0f}% WR), PnL {conv_pnl24:+.2f}")
        lines.append(f"Open: {conv_open_n} positions, ${conv_alloc:.0f} ({conv_alloc_pct:.1f}% of ${conv_cap:.0f})")
        lines.append(f"Roster: {conv_roster} wallets" + ("" if conv_enabled else "  (DISABLED)"))
        if ckc and ckc.n is not None:
            kt = ckc.kt or "[]"
            trig = "none" if kt in ("[]", "", None) else kt
            sharpe = ckc.sharpe if ckc.sharpe not in (None, "") else "n/a"
            wr_str = f"{float(ckc.wr)*100:.1f}%" if ckc.wr not in (None, "") else "n/a"
            lines.append(f"Window: n={ckc.n}, WR={wr_str}, NetPnL={ckc.net}%, Sharpe={sharpe}")
            lines.append(f"Triggers: {trig}")

    return "\n".join(lines)


def main() -> int:
    try:
        body = _build()
    except Exception:
        log.exception("daily_digest_build_failed")
        # Still send something so silence doesn't look like "all fine"
        body = "⚠ digest build failed — check the bot. (This message means the "
        body += "digest script erred, NOT necessarily that COPY is down.)"

    try:
        emit_alert(
            severity=Severity.P2,
            title="📊 COPY daily digest",
            body=body,
            bot_id="copy",
            event_type="copy_daily_digest",
            metadata={},
        )
        print("digest sent:\n" + body)
    except Exception:
        log.exception("daily_digest_emit_failed")
        print("ERROR: emit failed\n" + body, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
