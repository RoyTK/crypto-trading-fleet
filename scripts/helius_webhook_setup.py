"""Register / sync the Helius webhook for COPY bot.

One-shot script. Idempotent — re-running:
- creates the webhook if missing
- PATCHes the address list / transaction types if they drifted from the
  current wallet_pool.json
- noops if everything is in sync

Reads addresses from bots/copy/wallet_pool.json (Solana entries only).

Required env:
    HELIUS_API_KEY                    — Developer plan key
    HELIUS_WEBHOOK_AUTH_SECRET        — shared secret matching receiver
    COPY_WEBHOOK_URL                  — e.g. https://webhooks.royfleet.dev/copy/helius

Usage:
    docker compose exec framework python -m scripts.helius_webhook_setup
    docker compose exec framework python -m scripts.helius_webhook_setup --list
    docker compose exec framework python -m scripts.helius_webhook_setup --delete <webhook_id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

from bots.copy.venue.helius_webhooks import (
    WebhookConfig,
    delete_webhook,
    list_webhooks,
    sync_webhook,
)
from framework.audit import write_audit
from framework.logging_setup import configure_logging, get_logger


REPO_ROOT = Path(__file__).resolve().parent.parent
WALLET_POOL_PATH = REPO_ROOT / "bots" / "copy" / "wallet_pool.json"


def _load_pool_solana() -> list[str]:
    if not WALLET_POOL_PATH.exists():
        return []
    doc = json.loads(WALLET_POOL_PATH.read_text())
    return [
        e["address"] for e in (doc.get("wallets") or [])
        if e.get("chain") == "solana" and e.get("address")
    ]


async def _sync(log) -> int:
    api_key = os.environ.get("HELIUS_API_KEY", "")
    auth_secret = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    url = os.environ.get("COPY_WEBHOOK_URL", "")

    missing = [k for k, v in (
        ("HELIUS_API_KEY", api_key),
        ("HELIUS_WEBHOOK_AUTH_SECRET", auth_secret),
        ("COPY_WEBHOOK_URL", url),
    ) if not v]
    if missing:
        print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
        return 2

    addresses = _load_pool_solana()
    if not addresses:
        print(f"ERROR: no Solana addresses in {WALLET_POOL_PATH}", file=sys.stderr)
        return 2

    cfg = WebhookConfig(
        webhook_url=url,
        auth_header=auth_secret,
        addresses=addresses,
    )

    print(f"Pool size:        {len(addresses)} Solana addresses")
    print(f"Webhook URL:      {url}")
    print(f"Transaction types: {list(cfg.transaction_types)}")
    print()

    async with aiohttp.ClientSession() as session:
        webhook_id, action = await sync_webhook(session, api_key, cfg)

    print(f"Result: {action.upper()}  webhook_id={webhook_id}")
    write_audit(
        "copy_webhook_synced",
        bot_id="copy",
        payload={"webhook_id": webhook_id, "action": action,
                 "address_count": len(addresses), "url": url},
    )
    return 0


async def _list_only(log) -> int:
    api_key = os.environ.get("HELIUS_API_KEY", "")
    if not api_key:
        print("ERROR: HELIUS_API_KEY not set", file=sys.stderr)
        return 2
    async with aiohttp.ClientSession() as session:
        webhooks = await list_webhooks(session, api_key)
    if not webhooks:
        print("(no webhooks registered on this Helius account)")
        return 0
    for w in webhooks:
        wid = w.get("webhookID") or w.get("id") or "?"
        url = w.get("webhookURL") or "?"
        n_addrs = len(w.get("accountAddresses") or [])
        types = w.get("transactionTypes") or []
        print(f"{wid}  {url}  addresses={n_addrs}  types={types}")
    return 0


async def _delete(log, webhook_id: str) -> int:
    api_key = os.environ.get("HELIUS_API_KEY", "")
    if not api_key:
        print("ERROR: HELIUS_API_KEY not set", file=sys.stderr)
        return 2
    async with aiohttp.ClientSession() as session:
        await delete_webhook(session, api_key, webhook_id)
    print(f"deleted {webhook_id}")
    return 0


def main() -> None:
    configure_logging()
    log = get_logger("scripts.helius_webhook_setup")
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="List webhooks; do nothing else")
    g.add_argument("--delete", metavar="WEBHOOK_ID", help="Delete the named webhook")
    args = p.parse_args()

    if args.list:
        rc = asyncio.run(_list_only(log))
    elif args.delete:
        rc = asyncio.run(_delete(log, args.delete))
    else:
        rc = asyncio.run(_sync(log))
    sys.exit(rc)


if __name__ == "__main__":
    main()
