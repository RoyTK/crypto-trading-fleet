# CLAUDE.md — read this first

## ⛔ Ground rule: do not guess

**Do not guess. Do the work and find the truth before acting or recommending, and never
present a guess as fact.** This system trades (paper now, real later) on the conclusions you
report — verify against the code, the live DB, and on-chain/log data instead of inferring.
When you genuinely cannot verify something, say so explicitly and label it as an assumption.
(This rule exists because a confident-but-wrong claim here ["+$523 is inflated"] wasted Roy's
time and nearly drove a wrong decision — the attribution was a fair `pnl/cluster_size` share,
which one DB query proved.)


This is the **Crypto Trading Fleet** project: a paper-trading crypto bot fleet — now
**COPY-only** (STRUCTURE decommissioned 2026-06-25) on a Hetzner server. COPY runs two
strategies: **cluster** (3-wallet co-buy) and **conviction** (single-wallet accumulation).
It is complex with a lot of accumulated context that does **not** all live in the code.

## ⭐ Start every session by reading the memory index

Your persistent project memory lives at:

```
C:\Users\Roy\.claude\projects\c--Projects-CryptoTradingworkflow\memory\
```

**Read `MEMORY.md` there first.** It is the index (one line per memory) and links to the
detailed notes — project state, decisions, ops gotchas, and references (Helius dashboard
access, OneDrive vetting files, payment methods, etc.). The harness auto-loads `MEMORY.md`
each session, but **after a context compaction or a fresh/resumed session, re-read it** (and
the linked files relevant to the task) so you are current before acting.

When you learn something durable, write it to a memory file and add a one-line pointer in
`MEMORY.md`, following the conventions described in that folder. Prune/fix stale notes.

## Human maintenance manual

A human-facing manual lives in `docs/manual/` (Operator track 1.x, Engineer track 2.x,
Reference 3.x). Build it with `python scripts/build_manual.py` → `docs/MANUAL.md` (searchable)
+ `docs/MANUAL.html` (print) + `docs/ACCESS_SHEET.html` (landscape, hand-fill, **blank — no
secrets**). It's a living doc; update the relevant section + rebuild + commit when the system
changes. Docs-only changes do not trigger a deploy.

## Key current facts (verify against memory + live state before acting)

- **Paper + shadow only; real-money flags are OFF.** COPY ON, running **two strategies** —
  cluster (3-wallet co-buy) + conviction (single-wallet cumulative-accumulation trigger, own
  $10k paper bankroll, isolated metrics). **STRUCTURE DECOMMISSIONED 2026-06-25** (`c41835d`):
  was only "parked" but its container ran on as a zombie spamming `whale_poll_failed` + a
  cross-bot cron IndexError; removed from docker-compose + kill/dd monitors + scoring crons.
  Code retained in `bots/structure/` — revive = restore the compose block + 3 cron hooks.
- **Wallet pool is vetted-only**; KEEP → active directly; `copy_active_list_target` = 300.
- **Wallet discovery is ATTENDED** (run browser-Opus / the VS Code Claude extension by hand) —
  headless `claude -p` has no Claude-in-Chrome browser tools. Helius budget = 30M credits/mo
  (~$149); cost ≈ webhook deliveries from active+watch wallets.
- **Deploy = `git push` → Hetzner autopull (~60s).** `reset --hard` wipes untracked files, so
  commit (don't scp) anything the bot needs. Secrets live in the server `.env` (not in git).
- **Commit messages** end with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
  (established session pattern — keep unless told otherwise).

When in doubt, read the memory files and the manual before acting.
