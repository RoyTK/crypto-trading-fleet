"""COPY token post-mortem — reconstruct a token's buy/sell/rug lifecycle.

Bundles the manual queries we ran for the TRILL + NUT post-mortems
(2026-06-10) into one repeatable tool, and accumulates a findings log so
we can derive the metric that decides the recoup-tier question: COPY's
SILENT-RUG RATE (rugs with no preceding smart-money exit signal).

Why this exists: rug-timing research shows Solana rugs cluster in the
first minutes-to-hours of a fresh mint, but that population base rate
describes scam tokens COPY never trades. We need OUR subset's numbers.
This tool builds that dataset one token at a time.

Usage (run inside the framework container against the prod DB):

  # Forensic report for one token
  docker compose exec -T framework python -m scripts.token_post_mortem <MINT>

  # Record the rug outcome after checking Solscan (durable, in audit_log)
  docker compose exec -T framework python -m scripts.token_post_mortem <MINT> \
      --rugged --rug-at "2026-06-08 16:40" --note "creator pulled 266 WSOL"

  # Record a token that did NOT rug
  docker compose exec -T framework python -m scripts.token_post_mortem <MINT> --no-rug

  # Backfill the 29-token browser-Opus audit (2026-06-10) in one shot
  docker compose exec -T framework python -m scripts.token_post_mortem --backfill

  # Aggregate all recorded findings: rug rate, silent-rug rate, rug-by-age
  docker compose exec -T framework python -m scripts.token_post_mortem --summary

  # List recorded findings
  docker compose exec -T framework python -m scripts.token_post_mortem --list

Findings are stored as audit_log rows (event_type='token_post_mortem'),
deduped by latest-per-mint in --summary. No migration needed.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from sqlalchemy import text

from framework.db import session_scope
from framework.audit import write_audit


FINDING_EVENT = "token_post_mortem"
CT = "America/Chicago"


# The browser-Opus on-chain audit of 2026-06-10 (all 29 distinct tokens
# COPY had traded, verified on Solscan). (mint_prefix, rugged, label).
# Backfill matches each prefix to the full mint in our trades table.
_AUDIT_2026_06_10: list[tuple[str, bool, str]] = [
    # --- rugs ---
    ("9wC5f97tpVT2", True,  "NUT (Good Nut) — rug, +208% exit"),
    ("56jore829siC", True,  "TRILL (Trillionaire) — rug, +253% exit"),
    ("3b8XLvVZ",     True,  "Teletubby — rug, +74% exit"),
    ("7H8zef5X",     True,  "Scooby Doo — rug, +36% exit"),
    ("Dxmt6jQB",     True,  "NEO — rug, +21% exit"),
    ("EKi12cnj",     True,  "UBT (Universal Basic Token) — rug, peaked +70% closed -51%"),
    ("76U8SgQ6",     True,  "TRILLION (Doge Trillionaire) — rug, -29% [serial deployer ERbjHyBxd]"),
    ("GcD4kWRf",     True,  "Commiss (Commissions) — rug/DOA, -36%"),
    ("ALr1dmfT",     True,  "'20' (pack of cigarettes) — rug, -21% [serial deployer ERbjHyBxd]"),
    ("7qVULygE",     True,  "CRCW (Crypto) — rug, -14%"),
    ("GScAhxRk",     True,  "Bountycore (Bounty For Good) — rug, -12%"),
    ("BgWptcAo",     True,  "CHANCE (Chancecoin) — rug, -16%"),
    ("95wbdkEQ",     True,  "PS2 (PS2FICATION) — rug, -11%"),
    ("3k55EMSX",     True,  "IRA (Imaginary Retirement Acct) — rug, peaked +21% closed -12%"),
    ("Bp2NeSnn",     True,  "BELLO (Bello) — rug, -16%"),
    ("6xfBZwBb",     True,  "Nunu (Justice for Nunu) — rug, peaked +27% closed -10%"),
    ("3dWXHCEe",     True,  "Teracorn (Trillion Dollar Unicorn) — rug, -13%"),
    ("GRFb9HmG",     True,  "CHANCE dup (Chancecoin) — rug, peaked +60% closed -3%"),
    ("4VxCgtEh",     True,  "LMEOW — rug (fake 2007 holders, 88% one wallet), -19%"),
    # --- legit ---
    ("Dfh5DzRg",     False, "Pippin — LEGIT (50k holders), -$79"),
    ("pumpCmXq",     False, "PUMP (Pump.fun) — LEGIT, -$9.50"),
    ("9BB6NFEc",     False, "Fartcoin — LEGIT, -$0.23"),
    ("EKpQGSJt",     False, "WIF (dogwifhat) — LEGIT, -$15"),
    ("6p6xgHyF",     False, "TRUMP (Official Trump) — LEGIT, +$19"),
    ("7vfCXTUX",     False, "WETH (Wrapped Ether/Wormhole) — LEGIT, +$3.54"),
    ("Dz9mQ9Nz",     False, "USELESS — LEGIT, +$1.53"),
    ("cbbtcf3aa",    False, "cbBTC (Coinbase Wrapped BTC) — LEGIT, -$1.82"),
    ("4k3Dyjzv",     False, "RAY (Raydium) — LEGIT, open"),
    ("ukHH6c7m",     False, "BOME (Book of Meme) — LEGIT, open"),
]


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

_TRADES_SQL = """
SELECT id, mode, fill_status,
       entry_price, exit_price,
       ROUND(size_usd::numeric, 2)      AS size_usd,
       ROUND(pnl_usd::numeric, 2)       AS pnl_usd,
       ROUND(pnl_pct::numeric, 2)       AS pnl_pct,
       ROUND((sim_metadata->>'peak_pct_since_entry')::numeric, 1) AS peak_pct,
       sim_metadata->>'token_age_at_entry_hours' AS age_h,
       sim_metadata->>'top10_holder_pct'         AS top10,
       exit_reason,
       entry_at AT TIME ZONE :tz AS entry_ct,
       exit_at  AT TIME ZONE :tz AS exit_ct
FROM trades
WHERE bot_id = 'copy' AND asset = :mint
ORDER BY entry_at
"""

_CLUSTERS_SQL = """
SELECT detected_at AT TIME ZONE :tz AS detected_ct,
       direction, cluster_size,
       ROUND(cluster_total_notional_usd::numeric, 2) AS notional,
       wallet_tier, fired, suppressed_reason
FROM cluster_detections
WHERE token_mint = :mint
ORDER BY detected_at
"""

_SHADOW_SQL = """
SELECT cluster_size, wallet_tier,
       fired_at AT TIME ZONE :tz AS fired_ct,
       entry_price, price_30m, price_1h, price_4h, price_12h,
       ROUND(mfe_pct::numeric, 1) AS mfe,
       ROUND(mae_pct::numeric, 1) AS mae,
       cluster_wallets
FROM copy_signal_shadow_log
WHERE token_mint = :mint
ORDER BY fired_at
"""


def _report(mint: str, *, quiet: bool = False) -> dict[str, Any]:
    """Run the forensic queries; print the report unless quiet. Returns a
    dict of derived facts used when recording a finding."""
    def out(s: str = "") -> None:
        if not quiet:
            print(s)

    with session_scope() as s:
        trades = s.execute(text(_TRADES_SQL), {"mint": mint, "tz": CT}).all()
        clusters = s.execute(text(_CLUSTERS_SQL), {"mint": mint, "tz": CT}).all()
        shadow = s.execute(text(_SHADOW_SQL), {"mint": mint, "tz": CT}).all()

    out(f"\n=== TOKEN POST-MORTEM: {mint} ===\n")

    # --- Our trades ---
    out("OUR TRADES")
    if not trades:
        out("  (none — COPY never traded this token)")
    else:
        for t in trades:
            age = f"{float(t.age_h):.2f}h" if t.age_h is not None else "n/a (pre-capture)"
            conc = f"{float(t.top10) * 100:.0f}%" if t.top10 is not None else "n/a"
            out(f"  #{t.id} {t.mode}/{t.fill_status}  "
                f"pnl={_f(t.pnl_usd)}  pct={_f(t.pnl_pct)}%  peak={_f(t.peak_pct)}%  "
                f"age_at_entry={age}  top10={conc}  exit={t.exit_reason}")
            out(f"      entry {t.entry_ct}  ->  exit {t.exit_ct}")

    # --- Cluster signal timeline ---
    out("\nCLUSTER SIGNAL TIMELINE  (long=buy, exit=sell-cluster)")
    n_exit_total = 0
    n_exit_fired = 0
    if not clusters:
        out("  (no cluster detections recorded)")
    else:
        for c in clusters:
            if c.direction == "exit":
                n_exit_total += 1
                if c.fired:
                    n_exit_fired += 1
            mark = "FIRED " if c.fired else f"suppr({c.suppressed_reason})"
            out(f"  {c.detected_ct}  {c.direction:<5} sz={c.cluster_size} "
                f"${_f(c.notional):>10}  {c.wallet_tier:<7} {mark}")

    # --- Price trajectory ---
    out("\nPRICE TRAJECTORY  (shadow log)")
    if not shadow:
        out("  (no shadow-log row)")
    else:
        for r in shadow:
            out(f"  fired {r.fired_ct}  sz={r.cluster_size}  "
                f"MFE={_f(r.mfe)}%  MAE={_f(r.mae)}%")
            out(f"      entry={r.entry_price}  30m={r.price_30m}  1h={r.price_1h}  "
                f"4h={r.price_4h}  12h={r.price_12h}")
            if r.cluster_wallets:
                out(f"      wallets={r.cluster_wallets}")

    # --- Derived analysis ---
    had_exit_signal = n_exit_total > 0
    out("\nANALYSIS")
    out(f"  exit-cluster signals detected: {n_exit_total} "
        f"({n_exit_fired} fired, {n_exit_total - n_exit_fired} suppressed)")
    if had_exit_signal:
        out("  -> Smart money DID signal an exit. A working sell-cluster "
            "(post-Option-A) would act on it.")
        if n_exit_fired == 0:
            out("  -> NOTE: every exit signal was SUPPRESSED (pre-Option-A "
                "dedup bug). Option A now lets these fire.")
    else:
        out("  -> NO exit-cluster signal. If this token rugged, it was a "
            "SILENT rug — the case no exit mechanism can catch. These are "
            "what a recoup tier / fresh-token timeout would hedge.")
    out()

    return {
        "mint": mint,
        "n_trades": len(trades),
        "exit_signals_detected": n_exit_total,
        "exit_signals_fired": n_exit_fired,
        "had_exit_signal": had_exit_signal,
        "our_exit_reason": trades[0].exit_reason if trades else None,
        "our_pnl_usd": float(trades[0].pnl_usd) if trades and trades[0].pnl_usd is not None else None,
        "token_age_at_entry_hours": (
            float(trades[0].age_h) if trades and trades[0].age_h is not None else None
        ),
        "top10_holder_pct": (
            float(trades[0].top10) if trades and trades[0].top10 is not None else None
        ),
    }


def _record(mint: str, *, rugged: bool, rug_at: Optional[str],
            note: Optional[str], quiet: bool = False) -> None:
    facts = _report(mint, quiet=quiet)
    payload = {
        **facts,
        "rugged": rugged,
        "rug_at": rug_at,
        # silent_rug = rugged with no exit signal at all → unhedgeable by
        # any exit mechanism; the metric that decides the recoup-tier call.
        "silent_rug": bool(rugged and not facts["had_exit_signal"]),
    }
    fid = write_audit(FINDING_EVENT, bot_id="copy", actor="roy", payload=payload, note=note)
    verdict = "RUGGED" if rugged else "no-rug"
    silent = " (SILENT)" if payload["silent_rug"] else ""
    print(f"[finding #{fid}] {mint[:14]}…  {verdict}{silent}  signals={facts['exit_signals_detected']}")


def _backfill() -> None:
    """Record the 29-token browser-Opus audit (2026-06-10) as findings.
    Matches each audit prefix to the full mint in our trades table.
    Skips mints already recorded so re-running is safe."""
    with session_scope() as s:
        assets = [r.asset for r in s.execute(text(
            "SELECT DISTINCT asset FROM trades WHERE bot_id = 'copy'")).all()]
    existing = {x.get("mint") for x in _findings()}
    recorded = skipped = nomatch = 0
    print(f"\nBackfilling {len(_AUDIT_2026_06_10)} audited tokens...\n")
    for prefix, rugged, label in _AUDIT_2026_06_10:
        match = next((a for a in assets if a.startswith(prefix)), None)
        if match is None:
            print(f"  [no-match] {prefix:<14} ({label})")
            nomatch += 1
            continue
        if match in existing:
            print(f"  [exists]   {match[:14]}… ({label})")
            skipped += 1
            continue
        _record(match, rugged=rugged, rug_at=None,
                note=f"audit 2026-06-10: {label}", quiet=True)
        recorded += 1
    print(f"\nbackfill complete: {recorded} recorded, {skipped} already-present, "
          f"{nomatch} no-match\n")


def _findings() -> list[dict[str, Any]]:
    """Latest finding per mint (dedupe by re-records)."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT payload, created_at FROM audit_log "
            "WHERE event_type = :ev ORDER BY created_at"
        ), {"ev": FINDING_EVENT}).all()
    by_mint: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = r.payload or {}
        mint = p.get("mint")
        if mint:
            by_mint[mint] = p  # later overwrites earlier → latest wins
    return list(by_mint.values())


def _age_band(age_h: Optional[float]) -> str:
    if age_h is None:
        return "unknown"
    if age_h < 1:
        return "<1h"
    if age_h < 6:
        return "1-6h"
    if age_h < 24:
        return "6-24h"
    return ">24h"


def _summary() -> None:
    f = _findings()
    if not f:
        print("(no recorded findings yet — run --backfill or record tokens first)",
              file=sys.stderr)
        return
    n = len(f)
    rugged = [x for x in f if x.get("rugged")]
    silent = [x for x in rugged if x.get("silent_rug")]
    # PnL split — the inversion check (rugs vs legit)
    rug_pnl = sum(x["our_pnl_usd"] for x in rugged if x.get("our_pnl_usd") is not None)
    legit = [x for x in f if not x.get("rugged")]
    legit_pnl = sum(x["our_pnl_usd"] for x in legit if x.get("our_pnl_usd") is not None)

    print(f"\n=== POST-MORTEM SUMMARY  (n={n} tokens) ===\n")
    print(f"  rugged:            {len(rugged)}/{n}  ({_pct(len(rugged), n)})")
    print(f"  rugged w/ signal:  {len(rugged) - len(silent)}/{len(rugged) or 1}  "
          f"(sell-cluster can catch — the GOOD case)")
    print(f"  SILENT rugs:       {len(silent)}/{len(rugged) or 1}  "
          f"({_pct(len(silent), len(rugged))} of rugs)  <- decides recoup-tier need")
    print()
    print(f"  P&L on RUGS:       {rug_pnl:+.2f}   (the profit center if positive)")
    print(f"  P&L on LEGIT:      {legit_pnl:+.2f}")
    print()
    print("  RUG RATE BY TOKEN AGE AT ENTRY:")
    for b in ["<1h", "1-6h", "6-24h", ">24h", "unknown"]:
        in_band = [x for x in f if _age_band(x.get("token_age_at_entry_hours")) == b]
        rb = [x for x in in_band if x.get("rugged")]
        if in_band:
            print(f"    {b:<8} {len(rb):>2}/{len(in_band):<2} rugged  ({_pct(len(rb), len(in_band))})")
    print()
    if not silent:
        print("  READ: 0 silent rugs so far → fixed sell-cluster is the backstop; "
              "recoup tier may be unnecessary. Keep accumulating.")
    else:
        print(f"  READ: {len(silent)} silent rug(s) → sell-cluster cannot catch these. "
              "If the silent rate stays material, add a recoup tier / fresh-token timeout.")
    print()


def _list() -> None:
    f = _findings()
    if not f:
        print("(no recorded findings yet)", file=sys.stderr)
        return
    print(f"{'mint':<46} {'rugged':<7} {'silent':<7} {'signals':<8} {'age_h':<8} {'pnl_usd':<10}")
    print("-" * 90)
    for x in f:
        age = f"{x['token_age_at_entry_hours']:.2f}" if x.get("token_age_at_entry_hours") is not None else "n/a"
        pnl = f"{x['our_pnl_usd']:.2f}" if x.get("our_pnl_usd") is not None else "n/a"
        print(f"{x.get('mint',''):<46} {str(x.get('rugged')):<7} "
              f"{str(x.get('silent_rug')):<7} {x.get('exit_signals_detected',0):<8} "
              f"{age:<8} {pnl:<10}")


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def _f(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _pct(num: int, den: int) -> str:
    if not den:
        return "n/a"
    return f"{num / den * 100:.0f}%"


def main() -> int:
    p = argparse.ArgumentParser(description="COPY token post-mortem + rug-finding accumulator")
    p.add_argument("mint", nargs="?", help="Token mint to analyze")
    p.add_argument("--rugged", action="store_true", help="Record this token as rugged")
    p.add_argument("--no-rug", action="store_true", help="Record this token as NOT rugged")
    p.add_argument("--rug-at", default=None, help="Rug timestamp, e.g. '2026-06-08 16:40' (free-text)")
    p.add_argument("--note", default=None, help="Free-text note stored with the finding")
    p.add_argument("--backfill", action="store_true", help="Record the 2026-06-10 29-token audit")
    p.add_argument("--summary", action="store_true", help="Aggregate all recorded findings")
    p.add_argument("--list", action="store_true", help="List recorded findings")
    args = p.parse_args()

    if args.backfill:
        _backfill()
        return 0
    if args.summary:
        _summary()
        return 0
    if args.list:
        _list()
        return 0
    if not args.mint:
        p.error("mint is required unless --backfill/--summary/--list")
    if args.rugged and args.no_rug:
        p.error("--rugged and --no-rug are mutually exclusive")

    if args.rugged or args.no_rug:
        _record(args.mint, rugged=args.rugged, rug_at=args.rug_at, note=args.note)
    else:
        _report(args.mint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
