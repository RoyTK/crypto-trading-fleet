"""One-shot Solana keypair generator for the COPY bot.

Prints:
  - Public address (paste into project_wallet_inventory.md)
  - Base58 secret key (paste into Hetzner .env as COPY_SOLANA_PRIVATE_KEY)
  - JSON array of bytes (Solana CLI compatible — saves a backup .json
    file Roy can import to a Phantom or solana-keygen wallet for backup)

Usage:
  python -m scripts.generate_copy_keypair --backup-json-path ~/copy_bot_keypair.json

Safety:
  - Run ONCE on a trusted local machine; do NOT run on a public/shared host
  - The secret key is printed to stdout. Capture it immediately and clear
    the terminal history when done.
  - Save the JSON file to a USB/paper backup, then DELETE from disk.
  - The .env on Hetzner should be the ONLY long-lived digital location of
    this key. project_wallet_inventory.md gets the PUBLIC address only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Solana keypair for the COPY bot")
    parser.add_argument(
        "--backup-json-path", type=str, default=None,
        help="If set, write the Solana-CLI-format JSON array of secret-key bytes here. "
             "Recommended: a tmpfs path you delete after backing up.",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Confirm you understand the secret key will be printed to stdout. Required.",
    )
    args = parser.parse_args()

    if not args.confirm:
        print(
            "REFUSING TO RUN without --confirm. This script prints a Solana secret key "
            "to stdout. Re-run with --confirm only on a trusted local machine.",
            file=sys.stderr,
        )
        return 2

    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError:
        print(
            "ERROR: solders not installed. Run `pip install solders` first.",
            file=sys.stderr,
        )
        return 1

    kp = Keypair()
    pubkey_b58 = str(kp.pubkey())
    secret_b58 = str(kp)  # solders' __str__ on Keypair returns base58 of 64-byte secret
    secret_bytes = list(bytes(kp))

    print("=" * 72)
    print("COPY bot Solana keypair — DO NOT SHARE ANY OF THE BELOW")
    print("=" * 72)
    print(f"\nPUBLIC ADDRESS (paste to project_wallet_inventory.md):\n  {pubkey_b58}\n")
    print("BASE58 SECRET KEY (paste to Hetzner .env as COPY_SOLANA_PRIVATE_KEY):")
    print(f"  {secret_b58}\n")
    if args.backup_json_path:
        path = Path(args.backup_json_path).expanduser().resolve()
        path.write_text(json.dumps(secret_bytes))
        # 0600 — owner read/write only
        try:
            path.chmod(0o600)
        except Exception:
            pass
        print(f"BACKUP JSON written to: {path}")
        print(
            "  (Solana-CLI compatible: `solana-keygen verify <pubkey> --keypair "
            f"{path}` confirms a match. BACK UP THIS FILE TO PAPER/USB AND DELETE "
            "FROM DISK.)\n"
        )
    print("Funding plan:")
    print("  1. Send ~$50-100 USDC to the public address from Kraken/Coinbase")
    print("  2. Send ~0.05 SOL (~$10) for transaction fees + ATA rent")
    print("  3. Verify balance with: solana balance " + pubkey_b58)
    print(
        "  4. Once balance confirmed, flip COPY_LIVE_ENABLED=true on Hetzner "
        "to begin sampled shadow execution."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
