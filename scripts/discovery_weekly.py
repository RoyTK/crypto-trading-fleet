"""Weekly wallet discovery → API vetting → PROBATION (2026-11-17).

Replaces ~/pwmcp/weekly_autovet.py, whose headless page scraper has been blocked by Birdeye's
Cloudflare challenge since ~Sept 2026 (every wallet "no metrics rendered"; 0 wallets added while
the weekly alert just said "+0").

Pipeline:
  1. Candidates: wallets in prerun_accumulators that pre-bought >= MIN_RUNNERS runners and are not
     in wallet_pool yet (strongest recurrence first), capped by the active-pool headroom.
  2. Vet each through bots.copy.api_vetter (paid Birdeye API, recalibrated rules).
  3. Bot farms inside the batch: at most one wallet per group can enter; the rest are BUNDLE.
  4. Apply (Roy 2026-10-01: seek aggressively, promote only the proven):
       cluster: everything except junk -> tier 'active' + cluster status 'probation'
                (api_vetter.admit_cluster_probation — calibration showed no vet predicts our P&L;
                 it only reliably catches junk: MM bots, bot farms, realized < $5k)
       conviction KEEP_CONVICTION      -> conviction roster on probation (conviction_watch)
       junk                            -> tier 'pruned' (source api_vet_reject) so they aren't re-vetted
       ERROR                           -> untouched (retried next run)
     Every verdict + metrics is logged to wallet_vet_log for later calibration against our P&L.
  5. Helius sync; alert. P2 summary; P1 when the run fails (no successful vets or > 50% errors).

Run (framework container):  python -m scripts.discovery_weekly [--dry-run] [--max N]
"""
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from bots.copy.api_vetter import Birdeye, admit_cluster_probation, bundle_groups, vet_wallet
from bots.copy.config import get_copy_settings
from framework.db import session_scope

ACTIVE_TARGET = 500
MIN_RUNNERS = 3
DEFAULT_MAX = 120


def _alert(severity: str, title: str, body: str) -> None:
    try:
        from framework.alerts import emit_alert
        from monitoring.alerting.taxonomy import Severity
        emit_alert(severity=getattr(Severity, severity), title=title, body=body)
    except Exception as e:  # pragma: no cover
        print(f"[alert failed] {e}: {title}")


def candidates(limit: int) -> list[str]:
    with session_scope() as s:
        return [r[0] for r in s.execute(text("""
            SELECT wallet FROM prerun_accumulators
            WHERE wallet NOT IN (SELECT address FROM wallet_pool)
            GROUP BY wallet HAVING count(DISTINCT token) >= :m
            ORDER BY count(DISTINCT token) DESC, max(ingested_at) DESC LIMIT :n"""),
            {"m": MIN_RUNNERS, "n": limit})]


def apply(results: list[dict], tag: str) -> dict:
    from bots.copy.loop_helpers import set_entity_status
    now = datetime.now(timezone.utc)
    n = {"cluster_probation": 0, "conviction_probation": 0, "pruned": 0}
    with session_scope() as s:
        for r in results:
            a, cl, cv = r["address"], r["cluster"], r["conviction"]
            s.execute(text("""INSERT INTO wallet_vet_log (address, vetted_at, source, cluster_verdict,
                              conviction_verdict, reason, metrics) VALUES (:a, :t, :src, :cl, :cv, :why, :m)"""),
                      {"a": a, "t": now, "src": tag, "cl": cl, "cv": cv, "why": r.get("reason", ""),
                       "m": json.dumps(r.get("metrics") or {})})
            enter = admit_cluster_probation(r) or cv == "KEEP_CONVICTION"
            if enter:
                s.execute(text("""INSERT INTO wallet_pool (address, chain, tier, source, added_at, promoted_at)
                                  VALUES (:a, 'solana', 'active', :src, :t, :t) ON CONFLICT (address) DO NOTHING"""),
                          {"a": a, "src": tag, "t": now})
                if cv == "KEEP_CONVICTION":
                    s.execute(text("UPDATE wallet_pool SET conviction = true, conviction_at = :t, "
                                   "conviction_reason = :r WHERE address = :a"),
                              {"a": a, "t": now, "r": f"{tag}: API-vetted KEEP_CONVICTION (probation)"})
            elif cv not in ("UNCERTAIN", "ERROR"):
                s.execute(text("""INSERT INTO wallet_pool (address, chain, tier, source, added_at, demoted_at)
                                  VALUES (:a, 'solana', 'pruned', 'api_vet_reject', :t, :t)
                                  ON CONFLICT (address) DO NOTHING"""), {"a": a, "t": now})
                n["pruned"] += 1
        s.execute(text("COMMIT"))
    for r in results:   # statuses after the pool rows exist
        if admit_cluster_probation(r) or r["conviction"] == "KEEP_CONVICTION":
            set_entity_status("cluster", r["address"], "probation",
                              f"{tag}: new wallet ({r['cluster']}) — proves on cluster_watch")
            n["cluster_probation"] += 1
        if r["conviction"] == "KEEP_CONVICTION":
            set_entity_status("conviction", r["address"], "watch", f"probation: new wallet ({tag})")
            n["conviction_probation"] += 1
    return n


def main():
    dry = "--dry-run" in sys.argv
    mx = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else DEFAULT_MAX
    tag = f"api_vet_{datetime.now(timezone.utc):%Y%m%d}"
    with session_scope() as s:
        active = int(s.execute(text("SELECT count(*) FROM wallet_pool WHERE tier = 'active'")).scalar())
    headroom = max(0, ACTIVE_TARGET - active)
    cands = candidates(min(mx, headroom * 3)) if headroom else []
    print(f"[discovery] active={active} target={ACTIVE_TARGET} headroom={headroom} candidates={len(cands)} dry={dry}")
    if not cands:
        print("[discovery] nothing to vet")
        return
    be = Birdeye(get_copy_settings().birdeye_api_key)
    results = []
    for i, a in enumerate(cands, 1):
        r = vet_wallet(be, a)
        results.append(r)
        if i % 10 == 0:
            print(f"  {i}/{len(cands)} vetted ({be.calls} API calls)", flush=True)
    # bot farms within the batch: keep at most one wallet per group (the best realized)
    groups = bundle_groups({r["address"]: r.get("swaps") or [] for r in results})
    by = {r["address"]: r for r in results}
    for g in groups:
        ranked = sorted(g, key=lambda a: -(by[a].get("metrics") or {}).get("realized", 0))
        for a in ranked[1:]:
            by[a]["cluster"] = by[a]["conviction"] = "BUNDLE"
            by[a]["reason"] = f"bot farm with {ranked[0][:8]}"
    for r in results:
        r.pop("swaps", None)
    errors = sum(1 for r in results if r["cluster"] == "ERROR")
    tally = {}
    for r in results:
        tally[r["cluster"]] = tally.get(r["cluster"], 0) + 1
    cv_keep = sum(1 for r in results if r["conviction"] == "KEEP_CONVICTION")
    print(f"[discovery] cluster verdicts {tally}; conviction KEEP {cv_keep}; bot-farm groups {len(groups)}; "
          f"errors {errors}; API calls {be.calls}")
    for r in results:
        print(f"  {r['address']}  cluster={r['cluster']:10} conviction={r['conviction']:24} {r.get('reason', '')}")
    if dry:
        return
    n = apply([r for r in results if r["cluster"] != "ERROR"], tag)
    print(f"[discovery] applied: {n}")
    try:
        from scripts.apply_vetting_results import _sync_helius_from_db
        _sync_helius_from_db()
    except Exception as e:
        print(f"[discovery] helius sync failed: {e}")
    body = (f"vetted {len(results)} (errors {errors}); cluster probation +{n['cluster_probation']}, "
            f"conviction probation +{n['conviction_probation']}, pruned {n['pruned']}; "
            f"verdicts {tally}; active was {active}")
    if len(results) - errors == 0 or errors > len(results) / 2:
        _alert("P1", "[copy] wallet discovery FAILED", body)
    else:
        _alert("P2", "[copy] weekly wallet discovery", body)


if __name__ == "__main__":
    main()
