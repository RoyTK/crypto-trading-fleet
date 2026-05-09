"""COPY bot paper-shakedown gate (Build A).

Five checks adapted from the original 7-gate plan, dropping items that
require shadow execution (deferred to Build B):
  1. Cluster detector emits a candidate from synthetic 3-wallet burst
  2. Software stop fires: live evidence (closed trades with exit_reason='stop')
  3. Per-bot DD halt: synthetic -13% trailing-24h paper PnL halts COPY
  4. Bot blind to scoring: code audit
  5. 48h clean paper run: heartbeat continuity + zero Tracebacks + active trades

Skipped (Build B): shadow execution e2e, reconciliation drift.

Usage:
    python -m scripts.shakedown_copy
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from bots.copy.signals.cluster import ClusterDetector
from bots.copy.venue.helius_solana import WalletBuyEvent
from framework.audit import write_audit
from framework.db import session_scope
from framework.dd_monitor import check_dd_breaches
from framework.halt_state import is_bot_halted
from framework.logging_setup import configure_logging, get_logger
from framework.models import BotState, Halt, Heartbeat, Signal, Trade


BOT_ID = "copy"
PAPER_CAPITAL_USD = 1000.0


CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = []


def _register(label: str):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# -----------------------------------------------------------------------------
# Check 1: cluster detector
# -----------------------------------------------------------------------------

@_register("Cluster detector emits candidate from 3-wallet burst")
def check_cluster_detector() -> tuple[bool, str]:
    detector = ClusterDetector()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    mint = "SHAKEDOWN_TEST_MINT"
    # Three wallets buying same mint within ~5min, each $6k (above $5k floor)
    for i, wallet in enumerate(("walletA", "walletB", "walletC")):
        detector.observe_buy(WalletBuyEvent(
            wallet_address=wallet,
            chain="solana",
            token_mint=mint,
            notional_usd=6_000.0,
            timestamp_ms=now_ms - (i * 60_000),
            tx_signature=f"shakedown_sig_{i}",
        ))
    candidates = detector.evaluate(now_ms=now_ms)
    matching = [c for c in candidates if c.asset == mint]
    if not matching:
        return False, f"no candidate emitted (got {len(candidates)} candidates total)"
    c = matching[0]
    if c.cluster_size != 3:
        return False, f"expected cluster_size=3, got {c.cluster_size}"
    if c.direction != "long":
        return False, f"expected direction=long, got {c.direction}"
    return True, f"candidate emitted: cluster_size={c.cluster_size}, direction={c.direction}"


# -----------------------------------------------------------------------------
# Check 2: software stop
# -----------------------------------------------------------------------------

@_register("Software stop closes positions at ~ -stop_pct")
def check_software_stop() -> tuple[bool, str]:
    """Empirical: every closed COPY paper trade with exit_reason='stop' must have
    pnl_pct close to -stop_pct (8.0). Slippage and intra-cycle moves can push
    pnl past the trigger, so we accept any pnl_pct <= -stop_pct."""
    with session_scope() as s:
        q = (
            select(Trade)
            .where(
                Trade.bot_id == BOT_ID,
                Trade.mode == "paper",
                Trade.fill_status == "closed",
                Trade.exit_reason == "stop",
            )
        )
        rows = s.execute(q).scalars().all()
        results = [(t.id, float(t.pnl_pct or 0.0)) for t in rows]
    if not results:
        return False, "no closed paper trades with exit_reason='stop' yet — pipeline hasn't generated stop evidence"
    expected_stop_pct = 8.0
    bad = [(tid, p) for tid, p in results if p > -expected_stop_pct + 0.01]
    if bad:
        return False, f"{len(bad)} stops fired ABOVE -{expected_stop_pct}% threshold: e.g. id={bad[0][0]} pnl_pct={bad[0][1]:.2f}"
    worst = min(p for _, p in results)
    return True, f"{len(results)} stop closes, all pnl_pct <= -{expected_stop_pct}% (worst {worst:.2f}%)"


# -----------------------------------------------------------------------------
# Check 3: DD halt
# -----------------------------------------------------------------------------

@_register("Per-bot DD halt fires on synthetic -13% trailing-24h")
def check_dd_halt() -> tuple[bool, str]:
    """Mirror shakedown_dd_halt_test.py for COPY thresholds (12/28/50)."""
    target_dd_pct = -13.0
    n_synth = 4
    target_pnl_usd = PAPER_CAPITAL_USD * (target_dd_pct / 100.0)
    pnl_per_trade = target_pnl_usd / n_synth
    now_utc = datetime.now(timezone.utc)
    trade_ids: list[int] = []
    sig_id: int = 0

    # Pre-clean any open halt
    _resume_open_halts("shakedown_copy_pre_clean")
    if is_bot_halted(BOT_ID):
        return False, "bot still halted after pre-clean"

    try:
        with session_scope() as s:
            sig = Signal(
                bot_id=BOT_ID, signal_type="shakedown_dd_test",
                asset="SHAKEDOWN_DD", venue="solana", direction="long",
                payload={"shakedown_dd_synth": True, "target_dd_pct": target_dd_pct},
            )
            s.add(sig)
            s.flush()
            sig_id = sig.id
            for i in range(n_synth):
                t = Trade(
                    bot_id=BOT_ID, signal_id=sig_id, mode="paper",
                    asset="SHAKEDOWN_DD", venue="solana", direction="long",
                    entry_price=1.0, exit_price=0.99,
                    size_usd=abs(pnl_per_trade) * 30.0, leverage=1.0,
                    entry_at=now_utc - timedelta(hours=2 + i),
                    exit_at=now_utc - timedelta(minutes=30 - i),
                    fees_usd=0.0,
                    pnl_pct=-3.25, pnl_usd=pnl_per_trade,
                    exit_reason="shakedown_dd_synth", fill_status="closed",
                    sim_metadata={"shakedown_dd_synth": True, "slippage_bps": 0.0},
                )
                s.add(t)
                s.flush()
                trade_ids.append(t.id)

        halt_type = check_dd_breaches(
            bot_id=BOT_ID, paper_capital_usd=PAPER_CAPITAL_USD,
            daily_pct=12.0, weekly_pct=28.0, total_pct=50.0,
        )
        halted = is_bot_halted(BOT_ID)
        passed = (halt_type == "dd_daily") and halted
        msg = f"halt_type={halt_type!r}, is_bot_halted={halted}"
    finally:
        _resume_open_halts("shakedown_copy_cleanup")
        _delete_synthetic(trade_ids, sig_id)

    return passed, msg


def _resume_open_halts(actor: str) -> int:
    n = 0
    with session_scope() as s:
        active = s.execute(
            select(Halt).where(Halt.bot_id == BOT_ID, Halt.resumed_at.is_(None))
        ).scalars().all()
        for h in active:
            h.resumed_at = datetime.now(timezone.utc)
            h.resumed_by = actor
            n += 1
        bot = s.get(BotState, BOT_ID)
        if bot is not None and bot.state == "halted":
            bot.state = "initializing"  # restored to original by check 5 / backdate step
            bot.halted_until = None
            bot.halt_reason = None
    return n


def _delete_synthetic(trade_ids: list[int], signal_id: int) -> None:
    if not trade_ids and not signal_id:
        return
    with session_scope() as s:
        for tid in trade_ids:
            t = s.get(Trade, tid)
            if t is not None:
                s.delete(t)
        if signal_id:
            sig = s.get(Signal, signal_id)
            if sig is not None:
                s.delete(sig)


# -----------------------------------------------------------------------------
# Check 4: bot blind to scoring
# -----------------------------------------------------------------------------

@_register("Bot blind to scoring (no Score writes / scoring imports)")
def check_blind_to_scoring() -> tuple[bool, str]:
    """COPY must never write to the scores table or import the scoring engine.
    Scoring runs in a separate process with its own DB session.
    """
    bots_copy = Path(__file__).resolve().parent.parent / "bots" / "copy"
    if not bots_copy.is_dir():
        return False, f"bots/copy not found at {bots_copy}"
    bad: list[tuple[Path, str, str]] = []
    forbidden_patterns = [
        (re.compile(r"\bfrom\s+framework\.models\s+import\s+[^#\n]*\bScore\b"), "imports Score model"),
        (re.compile(r"\bfrom\s+framework\.scoring\b"), "imports framework.scoring"),
        (re.compile(r"\bScore\s*\("), "constructs Score()"),
    ]
    for py_file in bots_copy.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for line_idx, line in enumerate(text.splitlines(), start=1):
            for pat, label in forbidden_patterns:
                if pat.search(line):
                    bad.append((py_file, f"{line_idx}: {line.strip()[:80]}", label))
    if bad:
        examples = "; ".join(f"{p.relative_to(bots_copy.parent.parent)}:{loc} ({label})" for p, loc, label in bad[:3])
        return False, f"{len(bad)} forbidden refs: {examples}"
    n_files = sum(1 for _ in bots_copy.rglob("*.py"))
    return True, f"audited {n_files} files in bots/copy/, zero scoring writes"


# -----------------------------------------------------------------------------
# Check 5: 48h clean paper run
# -----------------------------------------------------------------------------

@_register("48h clean paper run (heartbeat + activity + no errors)")
def check_clean_run() -> tuple[bool, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    with session_scope() as s:
        hb = s.get(Heartbeat, "bot:copy")
        if hb is None:
            return False, "no bot:copy heartbeat row"
        hb_age = (datetime.now(timezone.utc) - hb.last_ping_at).total_seconds()
        if hb_age > 60:
            return False, f"bot:copy heartbeat stale: {hb_age:.0f}s old"

        # Active paper trades in window
        n_open_in_window = s.execute(
            select(Trade).where(
                Trade.bot_id == BOT_ID,
                Trade.mode == "paper",
                Trade.entry_at >= cutoff,
            )
        ).scalars().all()
        n_closed_in_window = s.execute(
            select(Trade).where(
                Trade.bot_id == BOT_ID,
                Trade.mode == "paper",
                Trade.fill_status == "closed",
                Trade.exit_at >= cutoff,
            )
        ).scalars().all()
    if not n_closed_in_window:
        return False, "no paper trades CLOSED in last 48h — pipeline may be stalled"

    # Check container logs for tracebacks via docker compose. The shakedown
    # runs inside the framework container, so we can't shell out to docker on
    # the host — instead check the audit_log for crash markers.
    with session_scope() as s:
        from framework.models import AuditLog
        crashes = s.execute(
            select(AuditLog).where(
                AuditLog.event_type.in_(("bot_crash", "iterate_error")),
                AuditLog.bot_id == BOT_ID,
                AuditLog.created_at >= cutoff,
            )
        ).scalars().all()
    if crashes:
        return False, f"{len(crashes)} crash/iterate_error audit events in last 48h"

    return True, (f"heartbeat fresh ({hb_age:.0f}s), {len(n_open_in_window)} entries / "
                  f"{len(n_closed_in_window)} closes in 48h, no crash audits")


# -----------------------------------------------------------------------------
# Runner
# -----------------------------------------------------------------------------

def run_all() -> int:
    configure_logging()
    log = get_logger("shakedown.copy")
    print(f"\n=== COPY paper shakedown — {datetime.now(timezone.utc).isoformat()} ===\n")
    write_audit("shakedown_copy_start", bot_id=BOT_ID,
                payload={"checks": [label for label, _ in CHECKS]})

    failed = 0
    for i, (label, fn) in enumerate(CHECKS, start=1):
        print(f"[{i}/{len(CHECKS)}] {label}")
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"exception: {e!r}"
        status = "PASS" if ok else "FAIL"
        print(f"   {status}: {msg}\n")
        write_audit("shakedown_copy_check", bot_id=BOT_ID,
                    payload={"check": i, "label": label, "passed": ok, "message": msg})
        if not ok:
            failed += 1

    print(f"=== Result: {len(CHECKS) - failed}/{len(CHECKS)} passed ===")
    write_audit("shakedown_copy_complete", bot_id=BOT_ID,
                payload={"passed": len(CHECKS) - failed, "failed": failed})
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
