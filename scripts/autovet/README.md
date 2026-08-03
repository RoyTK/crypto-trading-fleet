# Autonomous wallet vetting (`scripts/autovet/`)

Headless Birdeye wallet discovery + vetting that **replaces attended browser-Opus**. A real
headless Chromium (Playwright on Hetzner) drives Birdeye's wallet-analyzer page and reads the
same computed metrics browser-Opus read. Built 2026-08-02/03.

> **These files are version-controlled MIRRORS.** The *operational* copies run from
> `~/pwmcp/` on the Hetzner host (outside the repo, so autopull's `git reset --hard` can't wipe
> them). If you edit here and want the change live, copy it to `~/pwmcp/` on the server — the
> repo copy is not auto-deployed to `~/pwmcp`. Full context: memory
> `reference_autonomous_wallet_vetting` + `reference_browser_playwright`.

## Host prerequisites (Hetzner)
- Docker image `pwmcp:local` (node:22 + `@playwright/mcp` + `playwright install chromium`),
  built from `~/pwmcp/Dockerfile`. Playwright resolves via `NODE_PATH=/opt/pwmcp/node_modules`.
- Run pattern: `docker run --rm -e NODE_PATH=/opt/pwmcp/node_modules -v ~/pwmcp:/work -w /opt/pwmcp pwmcp:local node /work/<script>.js`

## The recipe (why it works)
Birdeye's Cloudflare issues a **session-based** managed challenge on the 2nd+ request in a
browser session. Defeated by launching a **fresh browser per wallet** (+ stealth flags, direct
deep-link, no homepage warm-up). ~18s/wallet, sequential, same IP is fine. The per-token
Traded-Tokens table and the `forge` XHR stay Cloudflare-blocked headless → no per-token
liquidity/hold data; substitutes are **instant-sell% ≥50 = TOO_FAST** and **txns >1M = MM-suspect**.

## Scripts
| file | role |
|---|---|
| `vet_prod.js` | The vetter. `node vet_prod.js <in_addrs.txt> <out.jsonl>`. Fresh browser per wallet, extracts realized/unrealized/multi-x/instant-sell/rug/txns/age, emits a KEEP/REJECT/TOO_FAST verdict per wallet. Resumable (skips addresses already in out). |
| `weekly_autovet.py` | The **cron** (host `0 9 * * 0`). Pulls ≤`MAX_VET` fresh recurring candidates (prerun_accumulators, n_runners≥3, not-in-pool) → vets → CLUSTER_A→active up to the 500 headroom → prunes rejects (`auto_vet_reject`, self-dedup) → P2 alert. Env: `MAX_VET` (150), `DRY=1`. |
| `classify.py` | Attended-run classifier. Joins vet results + prerun profile + team pairs → CLUSTER_A / CLUSTER_B(cosigner) / CONVICTION tags + team components. |
| `team_detect.py` | Team detector. Builds connected components from `tight_pairs.txt`. |
| `tight_pairs.sql` | The tight-timing team query (self-join prerun_accumulators on token, 15-min `first_buy` window, ≥3 shared runners). |

## Attended one-off run (discovery + vet + classify)
```bash
# 1. pull candidates (n_runners>=3, not in pool) -> fresh_addrs.txt + fresh_profile.txt  (see weekly_autovet.py pull SQL)
# 2. vet:      docker run ... node /work/vet_prod.js /work/fresh_addrs.txt /work/fresh_vet_results.jsonl
# 3. classify: python3 classify.py           # -> cluster_A.txt / cluster_B.txt / conviction_cand.txt
# 4. apply:    build a KEEP csv -> docker compose cp -> apply_vetting_results --file ...  (weekly_autovet.py does this end-to-end)
```

## Verdict thresholds (conservative, summary-only)
REJECT if realized ≤0, <$5k, bag-holder (|unreal loss|>realized), <3 multi-x, or brand-new(<7d).
TOO_FAST if instant-sell ≥50%. MM-suspect if txns >1M (held, not promoted). Else KEEP. High
rug% is CONFIRMATORY (fresh-mint rug-riders that bank + exit), never a reject.

## Provenance
Added wallets are tagged `auto_vetted_*` / `auto_vet_cron` in `wallet_pool.source` so autonomous
vetting quality can be compared to `browser_opus_vetted` via forward cluster attribution.
