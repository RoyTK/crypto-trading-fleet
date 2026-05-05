"""Helius WebSocket pricing verification spike.

Connects to Helius's atlas Enhanced WebSocket endpoint and subscribes to
~10 of our most-active wallets via `transactionSubscribe`. Captures events
for a configurable duration (default 1 hour), then projects the monthly
cost at our full 138-wallet pool size.

Goal: confirm Helius docs' claim of 20 credits/MB billing for WS, before
committing 1 day to building the production WS path.

Usage:
    HELIUS_API_KEY=<TEST_KEY> python -m scripts.helius_ws_verify
    HELIUS_API_KEY=<TEST_KEY> python -m scripts.helius_ws_verify --minutes 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp


# 10 of our most-active wallets per the Solscan verification pass
# (mix of hand-seeds + Birdeye top trader/volume entries — realistic event volume)
TEST_WALLETS = [
    "3cBB2ZyoNy8YEquSSzR2Rpggp9vcrfz4NcbCKHp7BzvT",  # WIF/PNUT, "seconds ago" active
    "2WfeaMXh31wc2kzXDQGuKAYCBrCs8J6vBCwdLjMs8Uzr",  # TRUMP+Fartcoin swing, $1.9M
    "24BLFjSAcUPPWs8F7nhwthfRPvh5mopNYfu5WXTkLChr",  # $8M Fartcoin DCA
    "AHdUMwfSsmdoFhq84XQVqnNhUU3iyN2hokkz6pFtqMnj",  # $10M USDC->SOL
    "Ah7SnagSabPNPvkcciYwqqyCR1ymMVW3C9tFvJea1kzV",  # FRED $1M->$7M
    "ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn",  # 139k trades (active bot)
    "5KJEmUWT474k49ZVZrteFYicZbT22sTvvNRRNk75hBpn",  # $3.95M PnL, 2,945 trades
    "Euu7J5JWmvoF4vZ5M2X583jx8c9MgES58xW9SBFKziqq",  # $2.3M, 10k trades
    "HFvU7JH5GbWSASkdjrjhtbKKyG2bpx9Rh8ZfNH8PiciX",  # $1.7M, 18k trades
    "31o3cjq1yr2ssTrAvXHEGa5MUPbViDQChocmwoL8ptWc",  # $3.6M PnL, top earner
]

# Helius's atlas endpoint hosts the Enhanced WebSocket (transactionSubscribe).
# Format: wss://atlas-mainnet.helius-rpc.com/?api-key=<key>
ATLAS_WS_BASE = "wss://atlas-mainnet.helius-rpc.com/"

# Monthly cost projection: agent reported 20 credits per 1 MB streamed
CREDITS_PER_MB = 20.0

# Helius Developer plan budget for context
DEVELOPER_MONTHLY_CREDITS = 10_000_000


async def verify(api_key: str, minutes: int) -> int:
    url = f"{ATLAS_WS_BASE}?api-key={api_key}"
    n_test_wallets = len(TEST_WALLETS)
    test_seconds = minutes * 60

    sub_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {
                "vote": False,
                "failed": False,
                "accountInclude": TEST_WALLETS,
            },
            {
                "commitment": "confirmed",
                "encoding": "jsonParsed",
                "transactionDetails": "full",
                "showRewards": False,
                "maxSupportedTransactionVersion": 0,
            },
        ],
    }

    print(f"Connecting to {ATLAS_WS_BASE} (api key {api_key[:6]}...{api_key[-4:]})")
    print(f"Subscribing to {n_test_wallets} wallets via transactionSubscribe")
    print(f"Test duration: {minutes} min")
    print()

    event_count = 0
    bytes_received = 0
    started = datetime.now(timezone.utc).timestamp()

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.ws_connect(
                url,
                heartbeat=30,
                max_msg_size=0,  # no payload size limit
            ) as ws:
                await ws.send_json(sub_msg)
                print(f"[{_now()}] Sent subscribe; waiting for ack + events...")

                deadline = started + test_seconds
                while True:
                    remaining = deadline - datetime.now(timezone.utc).timestamp()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        raw_bytes = len(msg.data.encode("utf-8"))
                        bytes_received += raw_bytes
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue

                        # First response is subscription ack: {"id":1,"result":<sub_id>}
                        if payload.get("id") == 1 and "result" in payload:
                            print(f"[{_now()}] Subscription confirmed, sub_id={payload['result']}")
                            continue

                        # Subsequent are notifications: method="transactionNotification"
                        if payload.get("method", "").endswith("Notification"):
                            event_count += 1
                            if event_count <= 3 or event_count % 50 == 0:
                                print(f"[{_now()}] Event #{event_count} "
                                      f"(payload {raw_bytes} bytes, total {bytes_received/1024:.1f} KB)")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        print(f"[{_now()}] WS closed: type={msg.type}, data={msg.data}")
                        break
        except aiohttp.WSServerHandshakeError as e:
            print(f"ERROR: WebSocket handshake failed (status={e.status}): {e.message}",
                  file=sys.stderr)
            print(f"  Likely cause: invalid API key or endpoint not on free tier",
                  file=sys.stderr)
            return 2
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    elapsed = datetime.now(timezone.utc).timestamp() - started
    mb_received = bytes_received / (1024 * 1024)
    credits_used = mb_received * CREDITS_PER_MB

    print()
    print("=" * 70)
    print(f"Test ran for {elapsed/60:.1f} min over {n_test_wallets} wallets")
    print(f"  Events received:  {event_count}")
    print(f"  Bytes received:   {bytes_received:,} ({mb_received:.4f} MB)")
    print(f"  Avg event size:   {bytes_received/max(event_count,1):.0f} bytes")
    print()
    print("Projection at full pool (138 wallets) for 30 days:")
    if elapsed > 0:
        scale_factor = (138 / n_test_wallets) * (30 * 86400 / elapsed)
        proj_events = int(event_count * scale_factor)
        proj_mb = mb_received * scale_factor
        proj_credits = proj_mb * CREDITS_PER_MB
        proj_pct_of_developer = proj_credits / DEVELOPER_MONTHLY_CREDITS * 100
        print(f"  Projected events/month:   {proj_events:,}")
        print(f"  Projected MB streamed:    {proj_mb:,.1f} MB")
        print(f"  Projected credits/month:  {proj_credits:,.0f}")
        print(f"  As % of Developer 10M:    {proj_pct_of_developer:.2f}%")
        print()
        if proj_pct_of_developer < 50:
            print("VERDICT: WS approach is VIABLE on Developer plan.")
        elif proj_pct_of_developer < 100:
            print("VERDICT: WS approach is BORDERLINE on Developer plan — reconsider.")
        else:
            print("VERDICT: WS approach EXCEEDS Developer plan budget — reconsider.")
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=int, default=60,
                   help="Duration of the verification spike. Default 60 min.")
    args = p.parse_args()

    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("ERROR: HELIUS_API_KEY not set in environment.", file=sys.stderr)
        print("Pass the new (free-tier test) key:", file=sys.stderr)
        print("  HELIUS_API_KEY=<new_key> python -m scripts.helius_ws_verify",
              file=sys.stderr)
        sys.exit(2)

    rc = asyncio.run(verify(api_key=api_key, minutes=args.minutes))
    sys.exit(rc)


if __name__ == "__main__":
    main()
