"""Ingest the OneDrive co-buyer JSONL corpus into Postgres (co_buyer_observations).

Run in the framework container against JSONL file(s) copied in from OneDrive
(docker compose cp). Upserts by dedup_key (token|wallet|first_buy) so re-ingesting
the same file is a no-op. Once ingested, the corpus joins our LIVE data
(wallet_swaps_log, trades, wallet_attributions) for enriched co-buyer analysis.

Usage (framework container):
  python -m scripts.ingest_co_buyer_obs --file /app/obs.jsonl
  python -m scripts.ingest_co_buyer_obs --dir /app/cobuyer      # all *.jsonl in a dir
  python -m scripts.ingest_co_buyer_obs --dir /app/cobuyer --teams   # also print teams
"""
from __future__ import annotations

import argparse
import json
import sys
from glob import glob
from pathlib import Path

from sqlalchemy import text

from framework.db import session_scope


def _rows(paths):
    for p in paths:
        for ln in Path(p).read_text(encoding="utf-8-sig").splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith("//"):
                continue
            try:
                yield json.loads(s)
            except Exception:
                pass


def _d(v):
    """Normalize a date-ish field to 'YYYY-MM-DD' or None."""
    if not v:
        return None
    s = str(v)[:10]
    return s or None


def _ingest(paths) -> tuple[int, int]:
    ins = skip = 0
    with session_scope() as s:
        for o in _rows(paths):
            tok = (o.get("token") or "").strip()
            w = (o.get("wallet") or "").strip()
            if not tok or not w:
                skip += 1
                continue
            fb = _d(o.get("first_buy"))
            key = f"{tok}|{w}|{fb or ''}"
            r = s.execute(text("""
                INSERT INTO co_buyer_observations
                  (dedup_key, token, symbol, run_date, wallet, first_buy, held_into_run,
                   entry_liq_usd, run_liq_usd, source, observed)
                VALUES (:k,:tok,:sym,:rd,:w,:fb,:held,:eliq,:rliq,:src,:obs)
                ON CONFLICT (dedup_key) DO NOTHING
            """), {"k": key, "tok": tok, "sym": o.get("symbol"), "rd": _d(o.get("run_date")),
                   "w": w, "fb": fb, "held": o.get("held_into_run"),
                   "eliq": o.get("entry_liq_usd"), "rliq": o.get("run_liq_usd"),
                   "src": o.get("source"), "obs": _d(o.get("observed"))})
            if r.rowcount:
                ins += 1
            else:
                skip += 1
        s.execute(text("COMMIT"))
    return ins, skip


def _print_teams(min_tokens: int) -> None:
    """Recurring teams from the corpus, cross-checked against live data."""
    with session_scope() as s:
        rows = s.execute(text("""
            WITH obs AS (
                SELECT token, wallet FROM co_buyer_observations
                WHERE held_into_run IS DISTINCT FROM false
                GROUP BY token, wallet
            ),
            pairs AS (
                SELECT a.wallet AS w1, b.wallet AS w2, count(*) AS n_tokens
                FROM obs a JOIN obs b ON a.token = b.token AND a.wallet < b.wallet
                GROUP BY a.wallet, b.wallet
                HAVING count(*) >= :mt
            )
            SELECT p.w1, p.w2, p.n_tokens,
                   (SELECT count(*) FROM wallet_pool wp WHERE wp.address=p.w1 AND wp.tier='active') AS w1_active,
                   (SELECT count(*) FROM wallet_pool wp WHERE wp.address=p.w2 AND wp.tier='active') AS w2_active
            FROM pairs p ORDER BY p.n_tokens DESC LIMIT 50
        """), {"mt": min_tokens}).all()
    print(f"\nrecurring teams (>= {min_tokens} shared followable tokens): {len(rows)}")
    for r in rows:
        print(f"  {r.w1[:8]}..+{r.w2[:8]}..  tokens={r.n_tokens}  "
              f"active=[{'Y' if r.w1_active else 'n'},{'Y' if r.w2_active else 'n'}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--dir")
    ap.add_argument("--teams", action="store_true", help="also print recurring teams (live-joined)")
    ap.add_argument("--min-tokens", type=int, default=2)
    a = ap.parse_args()
    paths = []
    if a.file:
        paths.append(a.file)
    if a.dir:
        # match *.jsonl AND OneDrive's *.jsonl.txt (web editor appends .txt)
        paths += sorted(set(glob(str(Path(a.dir) / "*.jsonl")) +
                            glob(str(Path(a.dir) / "*.jsonl*"))))
    if paths:
        ins, skip = _ingest(paths)
        print(f"ingested {ins} new rows, {skip} skipped/dupe (from {len(paths)} file(s))")
    elif not a.teams:
        print("no --file/--dir given", file=sys.stderr)
        return 2
    if a.teams:
        _print_teams(a.min_tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
