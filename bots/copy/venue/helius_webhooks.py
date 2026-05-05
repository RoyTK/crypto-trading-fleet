"""Helius webhook subscription management.

Idempotent CRUD against Helius's /v0/webhooks API:
- Find existing webhook for our COPY bot (matched by transactionTypes + URL)
- Create if missing, PATCH if address list drifted from current pool
- Returns webhook_id for record-keeping

Used by `scripts/helius_webhook_setup.py` for one-shot setup, and could be
called from CopyBot.on_start() if we want auto-sync on every restart
(currently not — manual sync via the script keeps it simple).

API reference: https://www.helius.dev/docs/api-reference/webhooks
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import aiohttp


HELIUS_API_BASE = "https://api.helius.xyz/v0"


@dataclass
class WebhookConfig:
    """Match criteria + content for our COPY-bot webhook."""
    webhook_url: str           # public HTTPS endpoint (webhooks.royfleet.dev/copy/helius)
    auth_header: str           # shared secret echoed back as Authorization
    addresses: list[str]
    transaction_types: tuple[str, ...] = ("SWAP", "SWAP_EXACT_OUT")
    webhook_type: str = "enhanced"


async def list_webhooks(session: aiohttp.ClientSession, api_key: str) -> list[dict]:
    url = f"{HELIUS_API_BASE}/webhooks"
    async with session.get(url, params={"api-key": api_key},
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        data = await r.json()
    return data if isinstance(data, list) else []


async def create_webhook(
    session: aiohttp.ClientSession,
    api_key: str,
    cfg: WebhookConfig,
) -> dict:
    url = f"{HELIUS_API_BASE}/webhooks"
    body = {
        "webhookURL": cfg.webhook_url,
        "transactionTypes": list(cfg.transaction_types),
        "accountAddresses": cfg.addresses,
        "webhookType": cfg.webhook_type,
        "authHeader": cfg.auth_header,
    }
    async with session.post(url, params={"api-key": api_key}, json=body,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status >= 400:
            txt = await r.text()
            raise RuntimeError(f"create_webhook failed status={r.status}: {txt[:300]}")
        return await r.json()


async def update_webhook(
    session: aiohttp.ClientSession,
    api_key: str,
    webhook_id: str,
    cfg: WebhookConfig,
) -> dict:
    url = f"{HELIUS_API_BASE}/webhooks/{webhook_id}"
    body = {
        "webhookURL": cfg.webhook_url,
        "transactionTypes": list(cfg.transaction_types),
        "accountAddresses": cfg.addresses,
        "webhookType": cfg.webhook_type,
        "authHeader": cfg.auth_header,
    }
    async with session.put(url, params={"api-key": api_key}, json=body,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status >= 400:
            txt = await r.text()
            raise RuntimeError(f"update_webhook failed status={r.status}: {txt[:300]}")
        return await r.json()


async def delete_webhook(
    session: aiohttp.ClientSession,
    api_key: str,
    webhook_id: str,
) -> None:
    url = f"{HELIUS_API_BASE}/webhooks/{webhook_id}"
    async with session.delete(url, params={"api-key": api_key},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status >= 400:
            txt = await r.text()
            raise RuntimeError(f"delete_webhook failed status={r.status}: {txt[:300]}")


def _find_matching(existing: list[dict], cfg: WebhookConfig) -> Optional[dict]:
    """Match by URL — we own one webhook per URL, so URL is a stable key."""
    for w in existing:
        if w.get("webhookURL") == cfg.webhook_url:
            return w
    return None


async def sync_webhook(
    session: aiohttp.ClientSession,
    api_key: str,
    cfg: WebhookConfig,
) -> tuple[str, str]:
    """Idempotent: create-or-update so the webhook matches `cfg`.

    Returns (webhook_id, action) where action ∈ {"created", "updated", "noop"}.
    """
    existing = await list_webhooks(session, api_key)
    match = _find_matching(existing, cfg)

    if match is None:
        result = await create_webhook(session, api_key, cfg)
        return (result.get("webhookID") or result.get("id") or "", "created")

    webhook_id = match.get("webhookID") or match.get("id") or ""
    current_addrs = set(match.get("accountAddresses") or [])
    target_addrs = set(cfg.addresses)
    current_types = set(match.get("transactionTypes") or [])
    target_types = set(cfg.transaction_types)

    if current_addrs == target_addrs and current_types == target_types:
        return (webhook_id, "noop")

    await update_webhook(session, api_key, webhook_id, cfg)
    return (webhook_id, "updated")
