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

Reconcile policy (append-only — never rewrites or reorders existing rows, with
one exception: repairing corruption, below):
  - address in ONEDRIVE but not REPO  -> append to REPO
  - address in REPO but not ONEDRIVE  -> append to ONEDRIVE
  - address in BOTH with DIFFERENT verdict -> CONFLICT: reported, NOT touched.
    (These are deliberate human/Claude overrides, e.g. a REJECT later flipped to
    KEEP on attribution evidence. Resolve conflicts by hand.)

Malformed-row hardening (2026-07-17 — a single fused line silently blocked ALL
syncs for 3 days; the 31-batch verdicts sat in OneDrive while the task ran red):
  - FUSED LINES (two rows glued by a lost trailing newline, the observed
    corruption) are auto-repaired: the lost newline is re-inserted at the
    date/address boundary. A repair is accepted ONLY if every resulting piece
    independently validates as a full 9-column row with a valid verdict; repair
    is only ever attempted on rows that already FAIL validation, so clean rows
    can never be altered. Under --apply the repaired lines are written back to
    the source file (healing it for future runs).
  - Rows that are still malformed after the repair attempt are QUARANTINED:
    excluded from the sync, reported loudly, and (under --apply) appended to
    vetted_watch_results.quarantine.txt next to the OneDrive file. Clean rows
    keep flowing — one bad row no longer stops the pipeline.
  - Duplicate addresses within one file are a WARNING, not a blocker: the LAST
    occurrence wins (append-only ledger semantics — later verdicts supersede).
  - The hard refuse (exit 3) remains ONLY for structural corruption: a broken
    header, or >25% of rows malformed (that's a wrong/mangled file, not a bad
    row — do not sync anything from it).

Exit codes: 0 = synced (possibly with quarantined rows — check the output),
2 = a file is missing, 3 = structural corruption, nothing synced.

Default is a DRY RUN (report only). Pass --apply to write.

Examples:
  python scripts/reconcile_vetted_results.py                 # dry run, default paths
  python scripts/reconcile_vetted_results.py --apply         # write the sync
  python scripts/reconcile_vetted_results.py --conviction-top 20
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_COLS = 9  # address,verdict,realized,unrealized,avg_hold,rug_pct,multi_x,reason,date
VALID_VERDICTS = {"KEEP", "REJECT", "TOO_FAST"}

# Fused line = row N's trailing date runs straight into row N+1's base58 address
# (lost newline on append). Re-insert the newline at that boundary. base58
# alphabet excludes 0, O, I, l; Solana addresses are 32-44 chars.
_FUSE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})(?=[1-9A-HJ-NP-Za-km-z]{32,44},)")
_STRUCTURAL_BAD_FRAC = 0.25  # more than this fraction malformed -> refuse whole file

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
    # Drop blank lines AND '#' comment lines — browser-Opus appends run-headers
    # like "# CLUSTER co-trader walk ... anchors=10 ..." between batches; those are
    # not data rows and must not be parsed/validated/synced (they have no address).
    body = [ln for ln in lines[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    return header, body


def _parse(line: str) -> list[str] | None:
    try:
        return next(csv.reader([line]))
    except Exception:
        return None


def _row_ok(fields: list[str] | None) -> bool:
    return (fields is not None and len(fields) == EXPECTED_COLS
            and fields[1].strip() in VALID_VERDICTS)


def try_repair_fused(line: str) -> list[str] | None:
    """Split a fused line back into its rows; None unless EVERY piece validates."""
    pieces = _FUSE_RE.sub(lambda m: m.group(1) + "\n", line).split("\n")
    if len(pieces) < 2:
        return None
    if all(_row_ok(_parse(p)) for p in pieces):
        return pieces
    return None


def partition(name: str, header: str, body: list[str]) -> dict:
    """Classify rows: clean (sync these), repairs (fused->pieces, pieces are clean),
    quarantine ((reason, raw line) — excluded), warnings, structural (refuse-all)."""
    out: dict = {"clean": [], "repairs": {}, "quarantine": [], "warnings": [], "structural": []}
    hdr = _parse(header)
    if hdr is None or len(hdr) != EXPECTED_COLS:
        out["structural"].append(f"{name}: header does not have {EXPECTED_COLS} columns")
        return out

    seen: dict[str, str] = {}  # addr -> line (last occurrence wins)

    def _accept(ln: str) -> None:
        a = addr_of(ln)
        if a in seen:
            if seen[a] == ln:
                out["warnings"].append(f"{name}: exact duplicate line for {a[:12]}… (kept once)")
            else:
                old_v, new_v = verdict_of(seen[a]), verdict_of(ln)
                tag = "CONFLICTING duplicate" if old_v != new_v else "duplicate"
                out["warnings"].append(
                    f"{name}: {tag} for {a[:12]}… ({old_v} -> {new_v}); LAST occurrence wins")
        seen[a] = ln

    for i, ln in enumerate(body, start=2):  # +2: 1-based, after header
        fields = _parse(ln)
        if _row_ok(fields):
            _accept(ln)
            continue
        pieces = try_repair_fused(ln)
        if pieces is not None:
            out["repairs"][ln] = pieces
            for p in pieces:
                _accept(p)
            continue
        if fields is None:
            reason = "unparseable CSV"
        elif len(fields) != EXPECTED_COLS:
            reason = f"{len(fields)} cols (expected {EXPECTED_COLS})"
        else:
            reason = f"bad verdict {fields[1].strip()!r}"
        out["quarantine"].append((f"{name} line {i}: {reason}", ln))

    out["clean"] = list(seen.values())
    n_bad = len(out["quarantine"])
    if body and n_bad / len(body) > _STRUCTURAL_BAD_FRAC and n_bad >= 8:
        out["structural"].append(
            f"{name}: {n_bad}/{len(body)} rows malformed (> {int(_STRUCTURAL_BAD_FRAC*100)}%)"
            " — file looks corrupt/wrong, refusing to sync ANY of it")
    return out


def _write_back_repairs(path: Path, repairs: dict[str, list[str]]) -> None:
    """Replace each fused line with its split rows, in place (heals the source)."""
    text = path.read_text(encoding="utf-8")
    for orig, pieces in repairs.items():
        text = text.replace(orig, "\n".join(pieces), 1)
    path.write_text(text, encoding="utf-8")


def _quarantine_write(qpath: Path, items: list[tuple[str, str]]) -> int:
    """Append quarantined rows not already recorded; return how many were new."""
    existing = qpath.read_text(encoding="utf-8") if qpath.exists() else ""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = [(reason, ln) for reason, ln in items if ln not in existing]
    if new:
        with qpath.open("a", encoding="utf-8") as f:
            for reason, ln in new:
                f.write(f"# {stamp}  {reason}\n{ln}\n")
    return len(new)


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
        r = _parse(ln)
        if r is None or len(r) < EXPECTED_COLS or r[1].strip() != "KEEP":
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

    parts = {"REPO": partition("REPO", repo_hdr, repo_body),
             "ONEDRIVE": partition("ONEDRIVE", od_hdr, od_body)}

    structural = parts["REPO"]["structural"] + parts["ONEDRIVE"]["structural"]
    if structural:
        print("REFUSING TO SYNC — structural corruption:", file=sys.stderr)
        for pr in structural:
            print("  -", pr, file=sys.stderr)
        return 3

    # Loud, non-blocking report of anything unusual.
    all_repairs = {name: pt["repairs"] for name, pt in parts.items() if pt["repairs"]}
    all_quar = [(name, reason, ln) for name, pt in parts.items() for reason, ln in pt["quarantine"]]
    for name, repairs in all_repairs.items():
        for orig, pieces in repairs.items():
            print(f"REPAIRED {name}: fused line split into {len(pieces)} rows "
                  f"({', '.join(addr_of(p)[:10] + '…' for p in pieces)})")
    for name, reason, ln in all_quar:
        print(f"QUARANTINED {reason}: {ln[:70]}", file=sys.stderr)
    for name, pt in parts.items():
        for w in pt["warnings"]:
            print(f"WARNING {w}")

    repo_clean, od_clean = parts["REPO"]["clean"], parts["ONEDRIVE"]["clean"]
    repo_v = {addr_of(l): verdict_of(l) for l in repo_clean}
    od_v = {addr_of(l): verdict_of(l) for l in od_clean}

    only_od = [l for l in od_clean if addr_of(l) not in repo_v]   # -> append to repo
    only_repo = [l for l in repo_clean if addr_of(l) not in od_v]  # -> append to onedrive
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

    nothing_to_sync = not only_od and not only_repo and not all_repairs
    if nothing_to_sync and not all_quar:
        print("\nIn sync. Nothing to do.")
        return 0

    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply to sync"
              + (" + write back repairs" if all_repairs else "")
              + (" + record quarantine" if all_quar else "") + ".")
        return 0

    # 1. Heal repaired (fused) lines in their source files BEFORE appending.
    if parts["REPO"]["repairs"]:
        _write_back_repairs(args.repo, parts["REPO"]["repairs"])
        print(f"HEALED {len(parts['REPO']['repairs'])} fused line(s) in repo file")
    if parts["ONEDRIVE"]["repairs"]:
        _write_back_repairs(args.onedrive, parts["ONEDRIVE"]["repairs"])
        print(f"HEALED {len(parts['ONEDRIVE']['repairs'])} fused line(s) in OneDrive file")

    # 2. Record quarantined rows next to the OneDrive file (visible to Roy + browser-Opus).
    if all_quar:
        qpath = args.onedrive.parent / "vetted_watch_results.quarantine.txt"
        n_new = _quarantine_write(qpath, [(f"{name}: {reason}", ln) for name, reason, ln in all_quar])
        print(f"QUARANTINE: {len(all_quar)} malformed row(s) excluded from sync"
              f" ({n_new} newly recorded in {qpath})")

    # 3. Append the missing clean rows.
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
