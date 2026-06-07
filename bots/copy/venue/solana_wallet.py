"""Solana wallet manager — keypair loading + transaction signing.

Loads a base58-encoded private key from COPY_SOLANA_PRIVATE_KEY env var
and exposes a thin signing interface. Lazy-imports `solders` so the rest
of the COPY bot can run even when the Solana signing deps aren't
installed (e.g. local dev without live execution).

Safety:
- The private key NEVER leaves the host the bot runs on. It is read from
  env at startup and held only in process memory.
- `is_wallet_available()` returns False if no key is configured — gates
  every executor path that would sign a transaction.
- Public address is logged on startup so we can audit which wallet the
  bot is signing for. Funding + balance checks happen out of band.
- Key generation lives in scripts/generate_copy_keypair.py, NOT here —
  this module never CREATES keys, only LOADS them.

Reference: 2026-06-06 design decision — generate a new bot-specific
keypair (analogous to the HL agent-wallet pattern), separate from the
Phantom personal wallet. Blast radius = bot wallet balance only.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from bots.copy.config import get_copy_settings
from framework.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class WalletHandle:
    """Opaque handle to a loaded Solana wallet. The `keypair` field is the
    raw solders Keypair object — kept Any-typed so this module is
    importable without solders installed."""
    pubkey_b58: str
    keypair: Any  # solders.keypair.Keypair, intentionally untyped at module level


@lru_cache(maxsize=1)
def load_wallet() -> Optional[WalletHandle]:
    """Load the configured COPY signing wallet. Returns None if no key
    is set or solders isn't installed.

    The result is cached for the process lifetime — keypairs are
    immutable and there's no point re-decoding the same secret on
    every call. Tests that need to reset state should call
    `load_wallet.cache_clear()`.
    """
    settings = get_copy_settings()
    secret = (settings.copy_solana_private_key or "").strip()
    if not secret:
        return None

    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError:
        log.warning(
            "solders_not_installed",
            hint="add solders to framework/requirements.txt to enable live execution",
        )
        return None

    try:
        # Phantom and `solana-keygen` both export secret keys as base58
        # of the 64-byte (secret + pubkey) blob. solders' from_base58_string
        # accepts that format directly.
        kp = Keypair.from_base58_string(secret)
    except Exception:
        log.exception("copy_wallet_decode_failed")
        return None

    pubkey_b58 = str(kp.pubkey())
    log.info("copy_wallet_loaded", pubkey=pubkey_b58)
    return WalletHandle(pubkey_b58=pubkey_b58, keypair=kp)


def is_wallet_available() -> bool:
    """Cheap gate used by executor paths before any signing work."""
    return load_wallet() is not None


def sign_versioned_transaction(tx_b64: str) -> Optional[str]:
    """Sign a base64-encoded VersionedTransaction returned by Jupiter
    `/swap`. Returns a base64-encoded signed transaction ready to send,
    or None on failure.

    Jupiter's swap endpoint returns a placeholder-signed
    VersionedTransaction. We deserialize, REPLACE the signature with one
    produced by our keypair, and re-serialize. The returned blob is what
    you pass to `sendTransaction` on the Solana RPC.
    """
    handle = load_wallet()
    if handle is None:
        return None
    try:
        import base64
        from solders.transaction import VersionedTransaction  # type: ignore
    except ImportError:
        return None

    try:
        raw = base64.b64decode(tx_b64)
        # solders parses the wire format and returns a VersionedTransaction
        # whose first signature slot is the fee payer (= our wallet).
        tx_unsigned = VersionedTransaction.from_bytes(raw)
        # Replace the placeholder signature(s) with our keypair's signature.
        # VersionedTransaction's constructor signs the message with the
        # provided keypair list.
        signed = VersionedTransaction(tx_unsigned.message, [handle.keypair])
        return base64.b64encode(bytes(signed)).decode("ascii")
    except Exception:
        log.exception("copy_wallet_sign_failed")
        return None


def public_key_b58() -> Optional[str]:
    """Return the public address string, or None if wallet not loaded."""
    h = load_wallet()
    return h.pubkey_b58 if h is not None else None
