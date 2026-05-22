"""Daily wallet discovery cron — adds new candidates to watch tier.

Three discovery modes, picked via --mode:
  birdeye-gainers   : top gainers + top losers from Birdeye leaderboard
  hot-token-traders : top traders of N currently-trending tokens
  counterparties    : counterparties of current active-tier wallets

Each mode inserts new candidates with tier='watch'. Existing addresses
are skipped (idempotent). Cap watch list at WATCH_LIST_CAP — once full,
new discoveries replace the lowest-events_30d watch wallet (only if the
new candidate is from a "fresh" mode; counterparties don't displace).

Validation (Cielo) is NOT done at discovery time — the daily promotion
cron handles it lazily at promotion time. This keeps discovery fast +
cheap (no Cielo credits burned on watch-tier candidates that may never
qualify for promotion).

Cron entries (varied UTC hours to catch global activity patterns):
  0 14 * * * --mode=birdeye-gainers
  0 22 * * * --mode=hot-token-traders
  0 6  * * * --mode=counterparties
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import aiohttp
from sqlalchemy import select, text

from bots.copy.venue.birdeye import BirdeyeClient
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import WalletPool


log = get_logger("wallet_pool_discovery")

WATCH_LIST_CAP = 1000
MAX_NEW_PER_RUN = 20  # Roy's parameter — throttle discovery to avoid spikes


def _existing_addresses() -> set[str]:
    """All addresses currently in wallet_pool (any tier — including pruned)."""
    with session_scope() as s:
        rows = s.execute(select(WalletPool.address)).all()
    return {r[0] for r in rows}


def _current_watch_count() -> int:
    with session_scope() as s:
        return s.execute(text(
            "SELECT COUNT(*) FROM wallet_pool WHERE tier='watch'"
        )).scalar() or 0


def _displace_weakest_watch_if_full() -> bool:
    """If watch list is at cap, drop the single weakest (lowest events_30d) wallet.

    Returns True if a displacement happened (caller can now add one new).
    """
    cap = WATCH_LIST_CAP
    if _current_watch_count() < cap:
        return True  # room exists, no displacement needed
    with session_scope() as s:
        # Pick weakest non-pinned watch wallet
        row = s.execute(text("""
            SELECT address FROM wallet_pool
            WHERE tier='watch' AND pinned = FALSE
            ORDER BY events_30d ASC, COALESCE(last_event_at, added_at) ASC
            LIMIT 1
        """)).first()
        if row is None:
            return False  # all are pinned, can't displace
        weakest_addr = row[0]
        s.execute(text(
            "UPDATE wallet_pool SET tier='pruned', demoted_at=NOW() WHERE address=:a"
        ), {"a": weakest_addr})
        log.info("displaced_for_capacity", address=weakest_addr)
    return True


def _insert_new(addresses: list[str], source: str, chain: str = "solana") -> int:
    """Insert new addresses into wallet_pool as watch-tier. Returns count inserted.

    Skips addresses already in wallet_pool (any tier). If a previously-pruned
    address is re-discovered, reactivates it to watch (matches re-discovery
    semantics).
    """
    if not addresses:
        return 0
    existing = _existing_addresses()
    fresh = [a for a in addresses if a not in existing]
    re_discovered = [a for a in addresses if a in existing]

    inserted = 0
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # Re-activate pruned candidates we've seen before
        for addr in re_discovered:
            w = s.get(WalletPool, addr)
            if w is None or w.tier != "pruned":
                continue
            # Make room if needed
            if not _displace_weakest_watch_if_full():
                continue
            w.tier = "watch"
            w.added_at = now  # fresh window for "drop after 90d silent"
            w.demoted_at = None
            inserted += 1
            log.info("rediscovered_pruned", address=addr, source=source)

        # Add newly-discovered candidates (capped by MAX_NEW_PER_RUN + headroom)
        room = max(0, MAX_NEW_PER_RUN - inserted)
        for addr in fresh[:room]:
            if not _displace_weakest_watch_if_full():
                break
            s.add(WalletPool(
                address=addr, chain=chain, tier="watch",
                added_at=now, source=source,
            ))
            inserted += 1
    return inserted


# ----------------------------------------------------------------------
# Discovery modes
# ----------------------------------------------------------------------

async def _mode_birdeye_gainers(client: BirdeyeClient) -> list[str]:
    """Top gainers + top losers in last 24h, deduped."""
    out: set[str] = set()
    try:
        for direction in ("gainers", "losers"):
            top = await client.gainers_losers(direction=direction, time_frame="24h", limit=50)
            for t in top:
                addr = t.get("address") or t.get("owner") or t.get("wallet")
                if addr and isinstance(addr, str):
                    out.add(addr)
    except Exception:
        log.exception("birdeye_gainers_fetch_failed")
    return list(out)


async def _mode_hot_token_traders(client: BirdeyeClient) -> list[str]:
    """For each currently-trending token, fetch its top traders.

    Uses Birdeye's `defi/v2/tokens/top_traders` (already wired in BirdeyeClient).
    Hot tokens chosen via gainers feed for simplicity (no separate trending API).
    """
    out: set[str] = set()
    try:
        gainers = await client.gainers_losers(direction="gainers", time_frame="24h", limit=5)
        for g in gainers:
            mint = g.get("address") or g.get("mint")
            if not isinstance(mint, str):
                continue
            try:
                traders = await client.token_top_traders(mint=mint, limit=20)
                for t in traders:
                    addr = t.get("owner") or t.get("address")
                    if addr and isinstance(addr, str):
                        out.add(addr)
            except Exception:
                log.warning("token_top_traders_failed", mint=mint)
    except Exception:
        log.exception("hot_token_traders_fetch_failed")
    return list(out)


async def _mode_counterparties() -> list[str]:
    """Currently a no-op for COPY — Solana on-chain counterparty extraction
    requires parsing every recent tx of every active wallet, which is
    expensive in Helius credits. Defer to a later iteration.

    For STRUCTURE-style counterparty discovery on HL, see
    scripts/curate_whales_v2.py:expand_via_counterparties.
    """
    log.info("counterparties_mode_skipped",
             reason="Solana counterparty extraction deferred")
    return []


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def _run(mode: str) -> int:
    from bots.copy.config import get_copy_settings
    settings = get_copy_settings()

    if mode == "birdeye-gainers":
        async with aiohttp.ClientSession() as session:
            client = BirdeyeClient(session=session, api_key=settings.birdeye_api_key)
            candidates = await _mode_birdeye_gainers(client)
        source = "birdeye_gainers"
    elif mode == "hot-token-traders":
        async with aiohttp.ClientSession() as session:
            client = BirdeyeClient(session=session, api_key=settings.birdeye_api_key)
            candidates = await _mode_hot_token_traders(client)
        source = "hot_token_traders"
    elif mode == "counterparties":
        candidates = await _mode_counterparties()
        source = "counterparties"
    else:
        log.error("unknown_mode", mode=mode)
        return 2

    log.info("discovery_candidates", mode=mode, n=len(candidates))
    n_added = _insert_new(candidates, source=source)
    log.info("discovery_done", mode=mode, n_added=n_added)
    write_audit(
        "wallet_pool_discovery",
        payload={"mode": mode, "candidates": len(candidates), "added": n_added},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", required=True,
        choices=["birdeye-gainers", "hot-token-traders", "counterparties"],
    )
    args = parser.parse_args()
    configure_logging()
    return asyncio.run(_run(args.mode))


if __name__ == "__main__":
    sys.exit(main())
