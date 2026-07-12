"""One-time Telegram session setup for the info-source research (Telethon).

Creates an authorized MTProto session file used READ-ONLY to search/read public
memecoin call channels (the Phase 1 lead/lag study + the Phase 2 live collector).

BEFORE RUNNING (~5 min, one time):
  1. Go to https://my.telegram.org -> "API development tools" -> create an app
     (any name/short name; platform "Desktop"). Note the api_id + api_hash.
  2. Run this script IN AN INTERACTIVE terminal (it prompts for phone + the
     login code Telegram sends you, and your 2FA password if you have one):

       python scripts/tg_session_setup.py --api-id 12345 --api-hash abcdef...

     (or set TG_API_ID / TG_API_HASH env vars and pass nothing)

The session file is written OUTSIDE the repo (default:
%USERPROFILE%/.tg_sessions/fleet_research.session) and must NEVER be committed
— it is a login credential. *.session is also gitignored as a belt-and-braces.
For the eventual Hetzner live collector, scp the session file to the server and
put TG_API_ID/TG_API_HASH in the server .env (same rules as other secrets).

Read-only etiquette (ban risk stays ~zero if we respect it):
  - public channels only, no messages sent, no mass joins
  - ~1 resolve/search per 2-3s; back off hard on FloodWaitError
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient

DEFAULT_SESSION_DIR = Path.home() / ".tg_sessions"
DEFAULT_SESSION = DEFAULT_SESSION_DIR / "fleet_research"


async def _smoke_test(client: TelegramClient) -> None:
    me = await client.get_me()
    print(f"\n[ok] logged in as: {me.first_name or ''} (@{me.username or 'no-username'}, id={me.id})")
    # Read one message from Telegram's own public channel — proves public-channel
    # read access works without joining.
    entity = await client.get_entity("telegram")
    async for msg in client.iter_messages(entity, limit=1):
        print(f"[ok] public-channel read works (latest @telegram msg id={msg.id}, "
              f"date={msg.date:%Y-%m-%d})")
    print("\nSession ready. The research scripts can now run non-interactively.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-id", type=int, default=int(os.environ.get("TG_API_ID", "0") or 0))
    ap.add_argument("--api-hash", default=os.environ.get("TG_API_HASH", ""))
    ap.add_argument("--session", default=str(DEFAULT_SESSION),
                    help=f"session file path, no extension (default {DEFAULT_SESSION})")
    args = ap.parse_args()

    if not args.api_id or not args.api_hash:
        print("Missing api credentials. Get them at https://my.telegram.org "
              "(API development tools), then pass --api-id/--api-hash or set "
              "TG_API_ID/TG_API_HASH.")
        return 1

    Path(args.session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(args.session, args.api_id, args.api_hash)

    async def run():
        # .start() drives the interactive phone -> code -> (2FA) flow on first
        # run; afterwards the saved session authenticates silently.
        await client.start()
        try:
            await _smoke_test(client)
        finally:
            await client.disconnect()

    asyncio.run(run())
    print(f"\nSession file: {args.session}.session  (do NOT commit; keep out of the repo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
