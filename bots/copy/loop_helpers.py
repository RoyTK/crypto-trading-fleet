"""Persistence helpers for the COPY main loop.

Mirrors bots/structure/loop_helpers.py with the COPY signal candidate type.
Bots write to `signals` and `trades` only — never `scores`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text

from bots.base.fill_simulator_base import SimulatedFill
from bots.copy.signals.base import SignalCandidate
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Signal, Trade, WalletAttribution


_log = get_logger(__name__)


BOT_ID = "copy"


@dataclass
class OpenPaperTrade:
    trade_id: int
    signal_id: int
    asset: str
    venue: str
    direction: str
    entry_price: float
    size_usd: float
    leverage: float
    entry_at: datetime
    stop_pct: Optional[float]
    take_profit_pct: Optional[float]
    timeout_hours: Optional[int]
    # Trailing-stop state, persisted across cycles in Trade.sim_metadata.
    # None until the first cycle observes a positive move.
    peak_pct_since_entry: Optional[float] = None


def persist_signal(candidate: SignalCandidate) -> int:
    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID,
            signal_type=candidate.signal_type,
            asset=candidate.asset,
            venue=candidate.venue,
            direction=candidate.direction,
            payload=candidate.payload,
        )
        s.add(sig)
        s.flush()
        return sig.id


def _classify_cluster_wallet_tier(wallets: list[str]) -> str:
    """Classify cluster's wallet tier for kill_criteria filtering.

    Returns 'active' iff ALL wallets in the cluster are tier='active' and
    non-pruned in wallet_pool. Otherwise 'mixed' (some watch-tier or unknown).

    Bug context (2026-05-29): persist_paper_trade was not setting wallet_tier
    in sim_metadata, so the kill_criteria_monitor's filter
    `sim_metadata->>'wallet_tier' = 'active'` never matched. Result: COPY
    silently bled ~$1k while N=0 in kill_criteria view. Fix: tag every trade.
    """
    if not wallets:
        return "unknown"
    from sqlalchemy import text
    with session_scope() as s:
        rows = s.execute(
            text("SELECT address, tier FROM wallet_pool WHERE address = ANY(:addrs)"),
            {"addrs": list(wallets)},
        ).all()
    if len(rows) < len(wallets):
        return "mixed"
    return "active" if all(r.tier == "active" for r in rows) else "mixed"


def persist_paper_trade(
    *,
    signal_id: int,
    candidate: SignalCandidate,
    sim_fill: SimulatedFill,
    notional_usd: float,
    leverage: float = 1.0,
) -> Optional[int]:
    wallets_payload = (candidate.payload or {}).get("wallets") or {}
    if isinstance(wallets_payload, dict):
        cluster_wallets = list(wallets_payload.keys())
    elif isinstance(wallets_payload, list):
        cluster_wallets = wallets_payload
    else:
        cluster_wallets = []
    wallet_tier = _classify_cluster_wallet_tier(cluster_wallets)

    with session_scope() as s:
        trade = Trade(
            bot_id=BOT_ID,
            signal_id=signal_id,
            mode="paper",
            asset=candidate.asset,
            venue=candidate.venue,
            direction=candidate.direction,
            entry_price=sim_fill.fill_price,
            size_usd=notional_usd if sim_fill.fill_price is not None else None,
            leverage=leverage,
            entry_at=datetime.now(timezone.utc) if sim_fill.fill_price is not None else None,
            fees_usd=sim_fill.fees_usd,
            fill_status="open" if sim_fill.fill_price is not None else "no_fill",
            sim_metadata={
                "slippage_bps": sim_fill.slippage_bps,
                "no_fill_reason": sim_fill.no_fill_reason,
                "fill_metadata": sim_fill.metadata,
                "stop_pct": candidate.stop_pct,
                "take_profit_pct": candidate.take_profit_pct,
                "timeout_hours": candidate.timeout_hours,
                "cluster_size": candidate.cluster_size,
                "wallet_tier": wallet_tier,
                "cluster_wallets": cluster_wallets,
            },
        )
        s.add(trade)
        s.flush()
        trade_id = trade.id
    write_audit(
        "paper_trade_opened" if sim_fill.fill_price is not None else "paper_trade_no_fill",
        bot_id=BOT_ID,
        payload={
            "trade_id": trade_id,
            "signal_id": signal_id,
            "asset": candidate.asset,
            "chain": candidate.chain,
            "direction": candidate.direction,
            "size_usd": notional_usd,
            "cluster_size": candidate.cluster_size,
        },
    )
    return trade_id


def list_open_paper_trades() -> list[OpenPaperTrade]:
    out: list[OpenPaperTrade] = []
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
        )
        for t in s.execute(q).scalars():
            md = t.sim_metadata or {}
            out.append(OpenPaperTrade(
                trade_id=t.id,
                signal_id=t.signal_id or 0,
                asset=t.asset,
                venue=t.venue,
                direction=t.direction,
                entry_price=float(t.entry_price or 0.0),
                size_usd=float(t.size_usd or 0.0),
                leverage=float(t.leverage or 1.0),
                entry_at=t.entry_at or t.created_at,
                stop_pct=md.get("stop_pct"),
                take_profit_pct=md.get("take_profit_pct"),
                timeout_hours=md.get("timeout_hours"),
                peak_pct_since_entry=_to_float_or_none(md.get("peak_pct_since_entry")),
            ))
    return out


def _to_float_or_none(v) -> Optional[float]:
    """Best-effort numeric coerce — JSON columns sometimes round-trip int/str."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update_trade_peak_pct(trade_id: int, peak_pct: float) -> None:
    """Persist peak_pct_since_entry into Trade.sim_metadata.

    Monotonic: only writes when the new value exceeds the existing one.
    Used by the trailing-stop machinery in both _manage_open_positions
    (paper) and _manage_open_real_trades (shadow/live) so a bot restart
    doesn't lose accumulated peak state — without persistence the
    trailing stop would reset to the current price after every restart
    and lock in profits too late (or never).
    """
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.fill_status != "open":
            return
        md = dict(t.sim_metadata or {})
        existing = _to_float_or_none(md.get("peak_pct_since_entry")) or float("-inf")
        if peak_pct > existing:
            md["peak_pct_since_entry"] = peak_pct
            t.sim_metadata = md


@dataclass
class OpenRealTrade:
    """Lightweight handle to an open shadow/live Trade row, surfaced from
    `list_open_real_trades` so the main loop's exit logic can drive the
    executor without re-reading the full Trade object."""
    trade_id: int
    mode: str  # 'shadow' | 'live'
    asset: str
    venue: str
    direction: str
    entry_price: float
    size_usd: float
    entry_at: datetime
    stop_pct: Optional[float]
    take_profit_pct: Optional[float]
    timeout_hours: Optional[int]
    peak_pct_since_entry: Optional[float] = None


def list_open_real_trades() -> list[OpenRealTrade]:
    """All currently-open COPY trades in shadow OR live mode. Paper trades
    use list_open_paper_trades (the close path is different — sim fill vs
    real swap)."""
    out: list[OpenRealTrade] = []
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode.in_(("shadow", "live")),
            Trade.fill_status == "open",
        )
        for t in s.execute(q).scalars():
            md = t.sim_metadata or {}
            out.append(OpenRealTrade(
                trade_id=t.id,
                mode=t.mode,
                asset=t.asset,
                venue=t.venue,
                direction=t.direction,
                entry_price=float(t.entry_price or 0.0),
                size_usd=float(t.size_usd or 0.0),
                entry_at=t.entry_at or t.created_at,
                stop_pct=md.get("stop_pct"),
                take_profit_pct=md.get("take_profit_pct"),
                timeout_hours=md.get("timeout_hours"),
                peak_pct_since_entry=_to_float_or_none(md.get("peak_pct_since_entry")),
            ))
    return out


def close_paper_trade(
    *,
    trade_id: int,
    exit_price: float,
    exit_fill: SimulatedFill,
    exit_reason: str,
) -> None:
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.fill_status != "open":
            return
        t.exit_price = exit_price
        t.exit_at = datetime.now(timezone.utc)
        t.exit_reason = exit_reason
        t.fill_status = "closed"
        t.fees_usd = float(t.fees_usd or 0.0) + exit_fill.fees_usd

        if t.entry_price and t.entry_price > 0:
            raw_pct = (exit_price - t.entry_price) / t.entry_price * 100.0
            if t.direction == "short":
                raw_pct = -raw_pct
            t.pnl_pct = raw_pct * float(t.leverage or 1.0)
            t.pnl_usd = float(t.size_usd or 0.0) * (t.pnl_pct / 100.0)
        md = dict(t.sim_metadata or {})
        md["exit_slippage_bps"] = exit_fill.slippage_bps
        t.sim_metadata = md
    write_audit(
        "paper_trade_closed",
        bot_id=BOT_ID,
        payload={
            "trade_id": trade_id,
            "exit_reason": exit_reason,
            "exit_price": exit_price,
        },
    )
    # Attribute the closed trade's PnL to the wallets that triggered the cluster.
    # Best-effort — failure must NOT break the close path.
    try:
        attribute_closed_trade(trade_id)
    except Exception:
        _log.exception("attribution_failed", trade_id=trade_id)


def compute_attribution_rows(
    trade_bot_id: str,
    trade_fill_status: str,
    trade_venue: str,
    trade_pnl_usd: Optional[float],
    trade_pnl_pct: Optional[float],
    signal_payload: Optional[dict],
    signal_venue: Optional[str],
) -> list[dict]:
    """Pure function: compute per-wallet attribution dicts from trade + signal data.

    Returns list of dicts ready to be inserted as WalletAttribution rows
    (without trade_id/signal_id which the DB caller fills in). Returns []
    on any rejection condition. Equal-share by default; per-wallet
    notional from signal_payload['wallet_notionals'] is included for
    future weighted-attribution variants.
    """
    if trade_bot_id != BOT_ID:
        return []
    if trade_fill_status != "closed":
        return []
    if trade_pnl_usd is None or trade_pnl_pct is None:
        return []
    if not signal_payload:
        return []
    wallets = signal_payload.get("wallets") or []
    cluster_size = int(signal_payload.get("cluster_size") or len(wallets))
    if not wallets or cluster_size == 0:
        return []
    wallet_notionals = signal_payload.get("wallet_notionals") or {}
    per_wallet_pnl = float(trade_pnl_usd) / cluster_size
    per_wallet_pct = float(trade_pnl_pct)
    chain = signal_venue or trade_venue or "solana"
    return [
        {
            "wallet_address": w,
            "chain": chain,
            "cluster_size": cluster_size,
            "attributed_pnl_usd": per_wallet_pnl,
            "attributed_pnl_pct": per_wallet_pct,
            "notional_contribution_usd": wallet_notionals.get(w),
        }
        for w in wallets
        if isinstance(w, str)
    ]


def attribute_closed_trade(trade_id: int) -> int:
    """Write per-wallet PnL attribution rows for a closed paper trade.

    Idempotent: skips if attribution rows already exist for this trade_id.
    Returns the number of rows written. Pure attribution math is in
    `compute_attribution_rows` for testability.
    """
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.signal_id is None:
            return 0

        # Idempotency check
        existing = s.execute(
            select(WalletAttribution.id).where(WalletAttribution.trade_id == trade_id).limit(1)
        ).first()
        if existing is not None:
            return 0

        sig = s.get(Signal, t.signal_id)
        if sig is None:
            return 0

        rows = compute_attribution_rows(
            trade_bot_id=t.bot_id,
            trade_fill_status=t.fill_status,
            trade_venue=t.venue,
            trade_pnl_usd=t.pnl_usd,
            trade_pnl_pct=t.pnl_pct,
            signal_payload=sig.payload,
            signal_venue=sig.venue,
        )
        n = 0
        for row in rows:
            attr = WalletAttribution(
                bot_id=BOT_ID,
                trade_id=trade_id,
                signal_id=t.signal_id,
                **row,
            )
            s.add(attr)
            n += 1
        return n


def panic_close_all_open() -> int:
    n = 0
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
        )
        for t in s.execute(q).scalars():
            t.fill_status = "closed"
            t.exit_reason = "panic"
            t.exit_at = datetime.now(timezone.utc)
            n += 1
    if n > 0:
        write_audit("panic_paper_trades_closed", bot_id=BOT_ID, payload={"count": n})
    return n


def has_open_position(asset: str, venue: str) -> bool:
    """Dedupe: true if we already have an open paper trade for this token."""
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
            Trade.asset == asset,
            Trade.venue == venue,
        )
        return s.execute(q).first() is not None


@dataclass(frozen=True)
class DedupResult:
    """Outcome of write_cluster_detection.

    fired=True means this is the first detection for the (chain, token,
    signal_type, direction, window_bucket) tuple within the dedup_hours
    window — caller should proceed with downstream actions (shadow_log,
    Redis publishes, paper trade). fired=False means the cluster was
    suppressed; a separate audit row was written with suppressed_reason.
    """
    fired: bool
    cluster_detection_id: Optional[int] = None
    reason: Optional[str] = None
    dedup_key: Optional[str] = None
    window_bucket: Optional[datetime] = None


def compute_window_bucket(detected_at: datetime, dedup_hours: int) -> datetime:
    """Floor detected_at to a dedup_hours-aligned UTC boundary.

    Pure function — no DB. 24h dedup_hours buckets to midnight UTC; 4h
    dedup_hours buckets to 00/04/08/12/16/20 UTC; 1h to the hour mark.
    Negative or zero dedup_hours degenerates to per-second bucket (no dedup).
    """
    if dedup_hours <= 0:
        return detected_at.replace(microsecond=0)
    ts_utc = detected_at.astimezone(timezone.utc) if detected_at.tzinfo else detected_at.replace(tzinfo=timezone.utc)
    # Anchor at midnight UTC of the same date, then add the floored hour offset.
    midnight = ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_offset = (ts_utc.hour // dedup_hours) * dedup_hours
    return midnight.replace(hour=hour_offset)


def compute_dedup_key(
    *,
    chain: str,
    token_mint: str,
    signal_type: str,
    direction: str,
    window_bucket: datetime,
) -> str:
    """sha256(chain|token|signal_type|direction|window_bucket.isoformat())."""
    raw = f"{chain}|{token_mint}|{signal_type}|{direction}|{window_bucket.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_cluster_detection(
    *,
    candidate: SignalCandidate,
    cluster_uuid: str,
    wallet_tier: str,
    dedup_hours: int,
    detected_at: Optional[datetime] = None,
) -> DedupResult:
    """Persist a cluster detection with atomic dedup.

    Writes one row with fired=true if no prior row exists for the same
    (chain, token, signal_type, direction, window_bucket). If a prior
    fired=true row exists, writes a fired=false row with
    suppressed_reason='dedup_window' instead, so we still have an audit
    trail of how many times the cluster re-detected within the window.

    Atomic: uses ON CONFLICT on the partial unique index
    uq_cluster_detections_dedup_key_fired (fired=true). The suppressed row
    is inserted unconditionally afterwards (the partial index does not
    constrain fired=false rows).

    Idempotent on cluster_uuid: if the same UUID is replayed (e.g., bot
    restart re-processes a stale event), the cluster_uuid UNIQUE constraint
    will reject the second write — caller treats that as fired=False.
    """
    detected_at = detected_at or datetime.now(timezone.utc)
    window_bucket = compute_window_bucket(detected_at, dedup_hours)
    dedup_key = compute_dedup_key(
        chain=candidate.chain,
        token_mint=candidate.asset,
        signal_type=candidate.signal_type,
        direction=candidate.direction,
        window_bucket=window_bucket,
    )
    total_notional = float((candidate.payload or {}).get("total_notional_usd", 0.0))

    insert_sql = text("""
        INSERT INTO cluster_detections (
            cluster_uuid, chain, token_mint, signal_type, direction,
            cluster_size, cluster_total_notional_usd, wallet_tier,
            window_bucket, dedup_hours, dedup_key,
            detected_at, fired, suppressed_reason
        ) VALUES (
            :cluster_uuid, :chain, :token_mint, :signal_type, :direction,
            :cluster_size, :cluster_total_notional_usd, :wallet_tier,
            :window_bucket, :dedup_hours, :dedup_key,
            :detected_at, :fired, :suppressed_reason
        )
        ON CONFLICT DO NOTHING
        RETURNING id
    """)
    base_params = {
        "chain": candidate.chain,
        "token_mint": candidate.asset,
        "signal_type": candidate.signal_type,
        "direction": candidate.direction,
        "cluster_size": int(candidate.cluster_size),
        "cluster_total_notional_usd": total_notional,
        "wallet_tier": wallet_tier,
        "window_bucket": window_bucket,
        "dedup_hours": int(dedup_hours),
        "dedup_key": dedup_key,
        "detected_at": detected_at,
    }

    with session_scope() as s:
        fire_params = {**base_params, "cluster_uuid": cluster_uuid, "fired": True, "suppressed_reason": None}
        row = s.execute(insert_sql, fire_params).first()
        if row is not None:
            return DedupResult(
                fired=True,
                cluster_detection_id=int(row.id),
                dedup_key=dedup_key,
                window_bucket=window_bucket,
            )

        suppressed_uuid = f"{cluster_uuid}.s"
        suppress_params = {
            **base_params,
            "cluster_uuid": suppressed_uuid,
            "fired": False,
            "suppressed_reason": "dedup_window",
        }
        s.execute(insert_sql, suppress_params)

    return DedupResult(
        fired=False,
        reason="dedup_window",
        dedup_key=dedup_key,
        window_bucket=window_bucket,
    )


def open_allocation_pct(paper_capital_usd: float) -> float:
    """Sum of size_usd of currently open paper trades, as % of paper capital."""
    if paper_capital_usd <= 0:
        return 0.0
    with session_scope() as s:
        from sqlalchemy import func
        total = s.execute(
            select(func.coalesce(func.sum(Trade.size_usd), 0.0)).where(
                Trade.bot_id == BOT_ID,
                Trade.mode == "paper",
                Trade.fill_status == "open",
            )
        ).scalar() or 0.0
    return float(total) / paper_capital_usd * 100.0
