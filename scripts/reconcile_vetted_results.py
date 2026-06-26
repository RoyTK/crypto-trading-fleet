#!/usr/bin/env python3
"""Reconcile the two copies of vetted_watch_results.txt.

There are two copies of the COPY wallet-vetting ledger, and they drift:

  - REPO     : bots/copy/vetted_watch_results.txt
               Git-tracked. This is the copy the bot actually ingests (the daily
               Hetzner cron does `docker compose cp` of this file into the
               framework container, then runs scripts.apply_vetting_results).
  - ONEDRIVE : C:\\Users\\Roy\\OneDrive\\Documents\\Claude\\vetted_watch_results.txt
               Where browser-Opus appends new vetting verdicts (it can't reach the
               git repo). Override with --onedrive or $VETTED_ONEDRIVE_PATH.

Browser-Opus appends to OneDrive; if those rows are never copied into the repo
file, the bot never sees them. This tool brings the two into sync.

IMPORTANT — this is a LOCAL tool. It must run on the machine that has BOTH files
(Roy's Windows box). The Hetzner daily cron CANNOT see OneDrive, so this is a
pre-commit step: run it locally, review, commit + push the repo file. It does
NOT git-commit for you.

Reconcile policy (append-only — never rewrites or reorders existing rows):
  - address in ONEDRIVE but not REPO  -> append to REPO
  - address in REPO but not ONEDRIVE  -> append to ONEDRIVE
  - address in BOTH with DIFFERENT verdict -> CONFLICT: reported, NOT touched.
    (These are deliberate human/Claude overrides, e.g. a REJECT later flipped to
    KEEP on attribution evidence. Resolve conflicts by hand.)

Default is a DRY RUN (report only). Pass --apply to write.

Examples:
  python scripts/reconcile_vetted_results.py                 # dry run, default paths
  python scripts/reconcile_vetted_results.py --apply         # write the sync
  python scripts/reconcile_vetted_results.py --conviction-top 20
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from pathlib import Path

EXPECTED_COLS = 9  # address,verdict,realized,unrealized,avg_hold,rug_pct,multi_x,reason,date
VALID_VERDICTS = {"KEEP", "REJECT", "TOO_FAST"}

_REPO_DEFAULT = Path(__file__).resolve().parent.parent / "bots" / "copy" / "vetted_watch_results.txt"
_ONEDRIVE_DEFAULT = os.environ.get(
    "VETTED_ONEDRIVE_PATH",
    r"C:\Users\Roy\OneDrive\Documents\Claude\vetted_watch_results.txt",
)


def addr_of(line: str) -> str:
    return line.split(",", 1)[0].strip()


def verdict_of(line: str) -> str:
    try:
        return next(csv.reader([line]))[1].strip()
    except Exception:
        return "?"


def read_rows(path: Path) -> tuple[str, list[str]]:
    """Return (header, body_lines) with blank lines dropped."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    header = lines[0] if lines else ""
    body = [ln for ln in lines[1:] if ln.strip()]
    return header, body


def validate(name: str, header: str, body: list[str]) -> list[str]:
    """Return a list of human-readable problems (empty = clean)."""
    problems: list[str] = []
    if len(next(csv.reader([header]))) != EXPECTED_COLS:
        problems.append(f"{name}: header does not have {EXPECTED_COLS} columns")
    seen: dict[str, int] = {}
    for i, ln in enumerate(body, start=2):  # +2: 1-based, after header
        try:
            fields = next(csv.reader([ln]))
        except Exception as e:  # pragma: no cover - defensive
            problems.append(f"{name} line {i}: unparseable ({e})")
            continue
        if len(fields) != EXPECTED_COLS:
            problems.append(f"{name} line {i}: {len(fields)} cols (expected {EXPECTED_COLS}): {ln[:60]}")
        v = fields[1].strip() if len(fields) > 1 else "?"
        if v not in VALID_VERDICTS:
            problems.append(f"{name} line {i}: bad verdict {v!r}")
        a = fields[0].strip()
        if a in seen:
            problems.append(f"{name} line {i}: duplicate address {a[:12]}… (also line {seen[a]})")
        else:
            seen[a] = i
    return problems


def _money(s: str) -> float | None:
    s = s.replace("$", "").replace("+", "").replace(",", "").strip()
    if not s or s.lower() in {"n/a", "na", "none", "?"}:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    mult = 1.0
    if s[-1:].upper() == "M":
        mult, s = 1e6, s[:-1]
    elif s[-1:].upper() == "K":
        mult, s = 1e3, s[:-1]
    try:
        v = float(s) * mult
    except ValueError:
        return None
    return -v if neg else v


def conviction_candidates(new_keep_lines: list[str], top: int) -> list[tuple]:
    rows = []
    for ln in new_keep_lines:
        try:
            r = next(csv.reader([ln]))
        except Exception:
            continue
        if len(r) < EXPECTED_COLS or r[1].strip() != "KEEP":
            continue
        realized = _money(r[2])
        if realized is None:
            continue
        rows.append((realized, _money(r[3]) or 0.0, r[0], r[4], r[6]))
    rows.sort(reverse=True)
    return rows[:top]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="repo vetted file")
    ap.add_argument("--onedrive", type=Path, default=Path(_ONEDRIVE_DEFAULT), help="OneDrive vetted file")
    ap.add_argument("--apply", action="store_true", help="write the sync (default: dry run)")
    ap.add_argument("--conviction-top", type=int, default=10, help="show top N conviction candidates from NEW KEEP rows")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    for label, p in (("REPO", args.repo), ("ONEDRIVE", args.onedrive)):
        if not p.exists():
            print(f"ERROR: {label} file not found: {p}", file=sys.stderr)
            if label == "ONEDRIVE":
                print("       (Run this on the local machine that has OneDrive, or pass --onedrive.)", file=sys.stderr)
            return 2

    repo_hdr, repo_body = read_rows(args.repo)
    od_hdr, od_body = read_rows(args.onedrive)

    problems = validate("REPO", repo_hdr, repo_body) + validate("ONEDRIVE", od_hdr, od_body)
    if problems:
        print("REFUSING TO SYNC — malformed rows found:", file=sys.stderr)
        for pr in problems:
            print("  -", pr, file=sys.stderr)
        return 3

    repo_v = {addr_of(l): verdict_of(l) for l in repo_body}
    od_v = {addr_of(l): verdict_of(l) for l in od_body}

    only_od = [l for l in od_body if addr_of(l) not in repo_v]   # -> append to repo
    only_repo = [l for l in repo_body if addr_of(l) not in od_v]  # -> append to onedrive
    conflicts = [(a, repo_v[a], od_v[a]) for a in repo_v.keys() & od_v.keys() if repo_v[a] != od_v[a]]

    print(f"REPO:     {len(repo_body)} rows  ({args.repo})")
    print(f"ONEDRIVE: {len(od_body)} rows  ({args.onedrive})")
    print(f"  OneDrive-only (-> repo):   {len(only_od)}  {dict(Counter(verdict_of(l) for l in only_od))}")
    print(f"  Repo-only     (-> onedrive): {len(only_repo)}  {dict(Counter(verdict_of(l) for l in only_repo))}")
    print(f"  Verdict conflicts (NOT auto-resolved): {len(conflicts)}")
    for a, rv, ov in conflicts:
        print(f"    CONFLICT {a[:14]}…  repo={rv}  onedrive={ov}")

    if not args.quiet and only_od:
        cands = conviction_candidates([l for l in only_od if verdict_of(l) == "KEEP"], args.conviction_top)
        if cands:
            print(f"\n  Conviction candidates among NEW KEEP rows (top {len(cands)} by realized):")
            for realized, unreal, a, hold, mx in cands:
                print(f"    {a[:12]}… realized=${realized:,.0f} unreal=${unreal:,.0f} hold={hold} mx={mx}")
            print("    (roster is curated via scripts/set_conviction_wallets.py — this is informational)")

    if not only_od and not only_repo:
        print("\nIn sync. Nothing to do.")
        return 0

    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply to sync.")
        return 0

    if only_od:
        with args.repo.open("a", encoding="utf-8") as f:
            for l in only_od:
                f.write(l + "\n")
    if only_repo:
        with args.onedrive.open("a", encoding="utf-8") as f:
            for l in only_repo:
                f.write(l + "\n")
    print(f"\nWROTE: +{len(only_od)} rows to repo, +{len(only_repo)} rows to onedrive.")
    print("Both files now hold the same address set. Commit + push the repo file:")
    print("  git add bots/copy/vetted_watch_results.txt && git commit && git push origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
