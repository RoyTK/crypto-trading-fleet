"""TG arm of the info-source study: earliest channel mention per runner token.

For each token in the 98-runner corpus (research/source_hits_2026-07-11.json) and
each candidate call channel, finds the EARLIEST message mentioning the token's
contract address (Telethon server-side search, oldest-first). Output feeds the
lead/lag join vs run_start / paid-promo / earliest accumulator buy.

READ-ONLY etiquette: public channels only, no joins (not needed for public
history), ~2.5s between searches, FloodWaitError honored exactly.

Creds: TG_API_ID/TG_API_HASH env vars, or %USERPROFILE%/.tg_sessions/
api_credentials.json ({"api_id": ..., "api_hash": "..."}). Session from
scripts/tg_session_setup.py at %USERPROFILE%/.tg_sessions/fleet_research.

Run LOCALLY (session lives on Roy's PC):
  python scripts/research_tg_leadlag.py --out research/tg_hits_2026-07-12.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError

SESSION = Path.home() / ".tg_sessions" / "fleet_research"
CREDS_FILE = Path.home() / ".tg_sessions" / "api_credentials.json"

CHANNELS = [
    # actively posting (2026-07 liveness check)
    "solana100xcall", "CallAnalyserSol", "CryptoAlphaGems", "HouseOfXeus",
    "solanagemcall", "FairLaunchAlpha",
    # stale but alive — history still searchable (PUMP_CALLS overlaps May corpus)
    "SolanaMemeCoinss", "PUMP_CALLS", "solalphacalls", "bestsolanamemecoinscalls",
    "SolanaGemCalls", "reversecat_sol",
    # no public preview — try resolving anyway (groups/restricted); skip on error
    "solcallbot", "pumpdotfunchat", "solanainsiders", "pumpfunalphacalls",
]

SEARCH_SLEEP = 2.5
RESOLVE_SLEEP = 3.0


def load_creds() -> tuple[int, str]:
    api_id = int(os.environ.get("TG_API_ID", "0") or 0)
    api_hash = os.environ.get("TG_API_HASH", "")
    if api_id and api_hash:
        return api_id, api_hash
    if CREDS_FILE.exists():
        d = json.loads(CREDS_FILE.read_text())
        return int(d["api_id"]), str(d["api_hash"])
    raise SystemExit(f"No TG creds: set TG_API_ID/TG_API_HASH or create {CREDS_FILE}")


def load_corpus(path: str) -> list[dict]:
    data = json.load(open(path))
    seen, out = set(), []
    for r in data:
        if r["token"] in seen:
            continue
        seen.add(r["token"])
        out.append({"token": r["token"], "symbol": r.get("symbol"),
                    "run_date": r["run_date"],
                    "earliest_accum_buy": r.get("earliest_accum_buy")})
    return out


async def earliest_mention(client: TelegramClient, entity, ca: str):
    """Oldest message containing the CA (server-side search, oldest-first)."""
    while True:
        try:
            async for msg in client.iter_messages(entity, search=ca, reverse=True, limit=1):
                return {"ts": int(msg.date.replace(tzinfo=timezone.utc).timestamp()),
                        "msg_id": msg.id}
            return None
        except FloodWaitError as e:
            print(f"    [flood-wait {e.seconds}s]")
            await asyncio.sleep(e.seconds + 2)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="research/source_hits_2026-07-11.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--channels", default="", help="comma-list override")
    args = ap.parse_args()

    api_id, api_hash = load_creds()
    corpus = load_corpus(args.corpus)
    channels = [c for c in (args.channels.split(",") if args.channels else CHANNELS) if c]
    print(f"corpus: {len(corpus)} tokens | channels to try: {len(channels)}")

    client = TelegramClient(str(SESSION), api_id, api_hash)
    await client.start()

    entities = {}
    for ch in channels:
        try:
            entities[ch] = await client.get_entity(ch)
            print(f"  resolved @{ch}")
        except (UsernameNotOccupiedError, ChannelPrivateError, ValueError) as e:
            print(f"  SKIP @{ch}: {type(e).__name__}")
        except FloodWaitError as e:
            print(f"  [flood-wait {e.seconds}s on resolve]")
            await asyncio.sleep(e.seconds + 2)
            try:
                entities[ch] = await client.get_entity(ch)
                print(f"  resolved @{ch}")
            except Exception as e2:
                print(f"  SKIP @{ch}: {type(e2).__name__}")
        await asyncio.sleep(RESOLVE_SLEEP)

    print(f"\nsearching {len(entities)} channels x {len(corpus)} tokens "
          f"(~{len(entities) * len(corpus) * SEARCH_SLEEP / 60:.0f} min)...")
    results = []
    t0 = time.time()
    for i, tok in enumerate(corpus):
        rec = {**tok, "mentions": {}}
        for ch, ent in entities.items():
            hit = await earliest_mention(client, ent, tok["token"])
            if hit:
                rec["mentions"][ch] = hit
            await asyncio.sleep(SEARCH_SLEEP)
        if rec["mentions"]:
            first_ch = min(rec["mentions"], key=lambda c: rec["mentions"][c]["ts"])
            print(f"  [{i+1}/{len(corpus)}] {tok['symbol']:<12} mentioned in "
                  f"{len(rec['mentions'])} ch, earliest @{first_ch}")
        results.append(rec)
        if (i + 1) % 10 == 0:
            el = time.time() - t0
            print(f"  -- {i+1}/{len(corpus)} tokens ({el/60:.0f} min elapsed) --")
            Path(args.out).write_text(json.dumps(results, indent=1))  # checkpoint

    Path(args.out).write_text(json.dumps(results, indent=1))
    n_hit = sum(1 for r in results if r["mentions"])
    print(f"\nDONE: {n_hit}/{len(results)} tokens mentioned in >=1 channel -> {args.out}")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
