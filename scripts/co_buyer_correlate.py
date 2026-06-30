"""Co-buyer corpus correlation — recurring accumulation TEAMS across tokens.

Reads the shared append-only JSONL corpus that browser-Opus writes
(OneDrive Claude/co_buyer_db/observations_*.jsonl — one object per followable
token x pre-run early buyer), derives the accumulation window, joins COPY's
vetting ledger, and finds wallet PAIRS that co-accumulated on >= MIN_TOKENS
followable tokens (held into the run) = recurring TEAMS. Members not already KEEP
in the ledger = co-buyers MISSED in prior curation.

Outputs (same folder, regenerated each run; do not hand-edit):
  teams.csv  — w1,w2,n_tokens,avg_accum_days,w1_status,w2_status,missed,tokens
  missed.csv — non-KEEP wallets recurring with our wallets (who we missed)

Pure stdlib (portable; runnable under Windows Task Scheduler with just python).
Read-only on the corpus. Override the OneDrive root with $CLAUDE_ONEDRIVE.

Usage:
  python scripts/co_buyer_correlate.py
"""
from __future__ import annotations

import csv
import itertools
import json
import os
from collections import defaultdict
from datetime import date
from glob import glob
from pathlib import Path

CLAUDE = Path(os.environ.get("CLAUDE_ONEDRIVE", r"C:\Users\Roy\OneDrive\Documents\Claude"))
SUBDIR = os.environ.get("CO_BUYER_SUBDIR", "co_buyer_db")
DB = CLAUDE / SUBDIR
LEDGER = CLAUDE / "vetted_watch_results.txt"
MIN_TOKENS = int(os.environ.get("CO_BUYER_MIN_TOKENS", "2"))


def _ledger_status() -> dict[str, str]:
    """address -> verdict (KEEP / REJECT / TOO_FAST)."""
    out: dict[str, str] = {}
    if not LEDGER.exists():
        return out
    for ln in LEDGER.read_text(encoding="utf-8-sig").splitlines()[1:]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        try:
            r = next(csv.reader([ln]))
        except Exception:
            continue
        if len(r) >= 2:
            out[r[0].strip()] = r[1].strip().upper()
    return out


def _load_obs() -> list[dict]:
    rows: list[dict] = []
    # match observations_*.jsonl AND OneDrive's observations_*.jsonl.txt (the web
    # editor appends .txt; the file is still JSONL content).
    for fp in sorted(set(glob(str(DB / "observations_*.jsonl")) +
                         glob(str(DB / "observations_*.jsonl*")))):
        for ln in Path(fp).read_text(encoding="utf-8-sig").splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith("//"):
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                pass  # skip malformed lines, don't abort the corpus
    return rows


def _accum_days(o: dict):
    try:
        fb = date.fromisoformat(str(o.get("first_buy"))[:10])
        rd = date.fromisoformat(str(o.get("run_date"))[:10])
        return (rd - fb).days
    except Exception:
        return None


def main() -> int:
    DB.mkdir(parents=True, exist_ok=True)
    obs = _load_obs()
    status = _ledger_status()

    # token -> {wallet: accum_days}  (exclude pre-run dumpers)
    tok: dict[str, dict[str, object]] = defaultdict(dict)
    for o in obs:
        w = (o.get("wallet") or "").strip()
        tk = (o.get("token") or "").strip()
        if not w or not tk:
            continue
        if o.get("held_into_run") is False:
            continue
        tok[tk][w] = _accum_days(o)

    pair_tok: dict[tuple, set] = defaultdict(set)
    pair_days: dict[tuple, list] = defaultdict(list)
    for tk, wd in tok.items():
        ws = sorted(wd)
        for a, b in itertools.combinations(ws, 2):
            pair_tok[(a, b)].add(tk)
            for w in (a, b):
                d = wd.get(w)
                if d is not None:
                    pair_days[(a, b)].append(d)

    teams = sorted(((p, t) for p, t in pair_tok.items() if len(t) >= MIN_TOKENS),
                   key=lambda x: -len(x[1]))

    with (DB / "teams.csv").open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["w1", "w2", "n_tokens", "avg_accum_days",
                     "w1_status", "w2_status", "missed", "tokens"])
        for (a, b), tks in teams:
            sa = status.get(a, "UNKNOWN")
            sb = status.get(b, "UNKNOWN")
            dd = pair_days[(a, b)]
            avg = round(sum(dd) / len(dd), 1) if dd else ""
            missed = "y" if (sa != "KEEP" or sb != "KEEP") else "n"
            wr.writerow([a, b, len(tks), avg, sa, sb, missed, ";".join(sorted(tks))])

    # missed rollup: each non-KEEP wallet -> how many teams/tokens + its KEEP partners
    miss: dict[str, dict] = defaultdict(lambda: {"tokens": set(), "partners": set(), "status": "UNKNOWN"})
    for (a, b), tks in teams:
        sa = status.get(a, "UNKNOWN")
        sb = status.get(b, "UNKNOWN")
        for w, sw, other, so in ((a, sa, b, sb), (b, sb, a, sa)):
            if sw != "KEEP":
                m = miss[w]
                m["status"] = sw
                m["tokens"].update(tks)
                if so == "KEEP":
                    m["partners"].add(other)

    with (DB / "missed.csv").open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["wallet", "status", "n_tokens", "n_keep_partners", "keep_partners"])
        for w, m in sorted(miss.items(), key=lambda kv: -len(kv[1]["tokens"])):
            wr.writerow([w, m["status"], len(m["tokens"]), len(m["partners"]),
                         ";".join(sorted(m["partners"]))])

    print(f"observations={len(obs)} followable_tokens={len(tok)} "
          f"teams(>= {MIN_TOKENS} tok)={len(teams)} missed_wallets={len(miss)}")
    print(f"wrote {DB / 'teams.csv'} and {DB / 'missed.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
