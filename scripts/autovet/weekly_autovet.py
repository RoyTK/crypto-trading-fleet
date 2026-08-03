#!/usr/bin/env python3
"""Weekly autonomous wallet discovery+vetting cron (Hetzner host).

Pipeline (idempotent, self-deduping):
  1. Pull up to MAX_VET fresh recurring candidates from prerun_accumulators
     (n_runners >= MIN_RUNNERS, NOT already in wallet_pool), capped at the
     active-pool headroom to ACTIVE_TARGET.
  2. Vet each on Birdeye via the headless Playwright vetter (vet_prod.js).
  3. CLUSTER_A (realized>=$5k, >=3 multi-x, not bag/fast/MM) -> active (apply_vetting_results + Helius sync).
  4. All other DEFINITIVE verdicts -> tier='pruned' source='auto_vet_reject'
     (so next week's "NOT IN wallet_pool" filter skips them = no re-vetting).
     ERROR/no-metrics rows are left alone (retried next run).
  5. Emit a summary + P2 alert.

Env overrides: MAX_VET (default 150), DRY=1 (classify+report only, no pool writes).
Run: python3 /home/fleet/pwmcp/weekly_autovet.py
"""
import json, os, subprocess, sys, csv

PW = "/home/fleet/pwmcp"
COMPOSE_DIR = "/home/fleet/crypto-fleet"
ACTIVE_TARGET = 500
MIN_RUNNERS = 3
MAX_VET = int(os.environ.get("MAX_VET", "150"))
DRY = os.environ.get("DRY", "") == "1"
IMG = "pwmcp:local"


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, **kw)


def psql(sql, tuples_only=True):
    args = ["/usr/bin/docker", "compose", "exec", "-T", "postgres",
            "psql", "-U", "fleet", "-d", "fleet"]
    if tuples_only:
        args += ["-tA"]
    args += ["-c", sql]
    r = sh(args)
    if r.returncode != 0:
        raise RuntimeError("psql failed: " + r.stderr[:300])
    return r.stdout.strip()


def log(m):
    print(m, flush=True)


def main():
    active = int(psql("SELECT COUNT(*) FROM wallet_pool WHERE tier='active';"))
    headroom = ACTIVE_TARGET - active
    log(f"[autovet] active={active} target={ACTIVE_TARGET} headroom={headroom} DRY={DRY}")
    if headroom <= 0:
        log("[autovet] pool at/over target; nothing to do.")
        return 0

    # 1. pull candidates
    pull = (
        "SELECT wallet||'|'||COUNT(DISTINCT token)||'|'||ROUND(AVG(n_buys)::numeric,1) "
        "FROM prerun_accumulators "
        "WHERE wallet NOT IN (SELECT address FROM wallet_pool) "
        f"GROUP BY wallet HAVING COUNT(DISTINCT token) >= {MIN_RUNNERS} "
        f"ORDER BY COUNT(DISTINCT token) DESC LIMIT {MAX_VET};"
    )
    rows = [r for r in psql(pull).splitlines() if r.strip()]
    addrs = [r.split("|")[0] for r in rows]
    with open(f"{PW}/cron_addrs.txt", "w") as f:
        f.write("\n".join(addrs) + "\n")
    log(f"[autovet] pulled {len(addrs)} fresh recurring candidates (n_runners>={MIN_RUNNERS})")
    if not addrs:
        log("[autovet] no fresh candidates; done.")
        return 0

    # 2. vet (blocking; ~18s/wallet)
    res = f"{PW}/cron_results.jsonl"
    if os.path.exists(res):
        os.remove(res)
    log(f"[autovet] vetting {len(addrs)} wallets (~{len(addrs)*18//60} min)...")
    r = sh(["/usr/bin/docker", "run", "--rm", "-e", "NODE_PATH=/opt/pwmcp/node_modules",
            "-v", f"{PW}:/work", "-w", "/opt/pwmcp", IMG, "node", "/work/vet_prod.js",
            "/work/cron_addrs.txt", "/work/cron_results.jsonl"])
    if r.returncode != 0:
        log("[autovet] VETTER FAILED: " + r.stderr[:300])
        return 1

    # 3. classify
    V = []
    for l in open(res):
        if l.strip():
            V.append(json.loads(l))
    def is_A(m):
        rz = m.get("realized"); tx = m.get("txns") or 0; un = m.get("unrealized")
        ins = m.get("instant_sell"); mx = m.get("multi_x", 0)
        if rz is None or m.get("verdict") == "ERROR":
            return None  # unknown -> skip, retry next week
        if tx > 1_000_000: return False
        if ins is not None and ins >= 50: return False
        if rz < 5000: return False
        if un is not None and un < 0 and abs(un) > rz: return False
        if mx < 3: return False
        return True
    keep, reject, skip = [], [], []
    for m in V:
        v = is_A(m)
        if v is True: keep.append(m)
        elif v is False: reject.append(m["address"])
        else: skip.append(m["address"])
    keep.sort(key=lambda m: -(m.get("realized") or 0))
    keep = keep[:headroom]  # cap at headroom
    log(f"[autovet] vetted={len(V)}  CLUSTER_A(keep)={len(keep)}  reject={len(reject)}  skip/error={len(skip)}")

    if DRY:
        log("[autovet] DRY run — no pool writes. Top keeps: " +
            ", ".join(f"{m['address'][:8]}(${(m.get('realized') or 0):,.0f})" for m in keep[:10]))
        return 0

    # 4a. apply CLUSTER_A -> active
    if keep:
        csvp = f"{PW}/cron_clusterA.csv"
        with open(csvp, "w", newline="") as f:
            w = csv.writer(f)
            for m in keep:
                w.writerow([m["address"], "KEEP", m.get("realized") or "", m.get("unrealized") or "",
                            m.get("age_str") or "", m.get("scam_rug") or "", m.get("multi_x") or 0,
                            "auto-vet weekly", "cron"])
        sh(["/usr/bin/docker", "compose", "cp", csvp, "framework:/tmp/cron_clusterA.csv"])
        ar = sh(["/usr/bin/docker", "compose", "exec", "-T", "framework", "python", "-m",
                 "scripts.apply_vetting_results", "--file", "/tmp/cron_clusterA.csv"])
        log("[autovet] apply: " + (ar.stdout.strip().splitlines() or ["(no output)"])[-1])
        # retag for provenance
        vals = ",".join("'" + m["address"] + "'" for m in keep)
        psql(f"UPDATE wallet_pool SET source='auto_vet_cron' WHERE address IN ({vals}) AND tier='active';")

    # 4b. persist rejects -> pruned (idempotency)
    if reject:
        vals = ",".join(f"('{a}','solana','pruned','auto_vet_reject',NOW())" for a in reject)
        psql("INSERT INTO wallet_pool (address, chain, tier, source, added_at) VALUES "
             + vals + " ON CONFLICT (address) DO NOTHING;")
    log(f"[autovet] DONE: +{len(keep)} active, {len(reject)} pruned (reject), {len(skip)} skipped(retry)")

    # 5. alert
    try:
        body = f"weekly auto-vet: +{len(keep)} active, {len(reject)} pruned, {len(skip)} retry. active now ~{active+len(keep)}/{ACTIVE_TARGET}"
        sh(["/usr/bin/docker", "compose", "exec", "-T", "framework", "python", "-c",
            "from framework.alerts import emit_alert; from monitoring.alerting.taxonomy import Severity; "
            f"emit_alert(severity=Severity.P2, title='[copy] weekly auto-vet', body={body!r}, bot_id='copy', event_type='auto_vet_weekly')"])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
