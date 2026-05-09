"""Backfill wallet PnL attributions for closed COPY paper trades.

Run once after the wallet_attributions table is created (alembic upgrade
0002) to populate rows for the 11+ already-closed COPY paper trades from
the $5k-floor era. After this, ongoing closures auto-attribute via the
hook in close_paper_trade.

Usage:
    docker compose exec -T framework python -m scripts.backfill_wallet_attributions
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from bots.copy.loop_helpers import attribute_closed_trade
from framework.db import session_scope
from framework.logging_setup import configure_logging
from framework.models import Trade


def main() -> int:
    configure_logging()
    with session_scope() as s:
        rows = s.execute(
            select(Trade.id).where(
                Trade.bot_id == "copy",
                Trade.mode == "paper",
                Trade.fill_status == "closed",
                Trade.pnl_usd.is_not(None),
            ).order_by(Trade.id)
        ).all()
        trade_ids = [r[0] for r in rows]

    print(f"Backfilling attributions for {len(trade_ids)} closed COPY paper trades...",
          file=sys.stderr)

    total_attr = 0
    n_with_attr = 0
    n_skipped = 0
    for tid in trade_ids:
        try:
            n = attribute_closed_trade(tid)
        except Exception as e:
            print(f"  trade {tid}: ERROR {e!r}", file=sys.stderr)
            continue
        if n > 0:
            n_with_attr += 1
            total_attr += n
        else:
            n_skipped += 1

    print(f"\nDone. {n_with_attr} trades attributed ({total_attr} total rows). "
          f"{n_skipped} skipped (already attributed, no signal, or no wallets in payload).",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
