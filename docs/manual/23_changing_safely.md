## Changing Safely

_Last reviewed: 2026-07-02_

The system is a **measurement experiment**. Careless changes either corrupt the
measurement or risk real money. These are the guardrails.

### The kill-criteria window lock

Each bot is being scored over a fixed window (see `framework/kill_criteria_monitor.py` and
the `config.py` headers). **The strategy constants in `bots/*/config.py` are LOCKED during
the window.**

- Changing a locked threshold **resets the measurement clock** — unless the change is a
  legitimate data-driven correction that meets ALL of: (1) justification written to
  `audit_log` *before* the change, (2) it names the observation forcing it, (3) it
  *shrinks* behavior space (more conservative, not more permissive), (4) at most once per
  parameter per window.
- Changes that do **not** reset the window: bug fixes, logging/dashboards/monitoring,
  docs, and the wallet pool churning through its normal tier rules.
- **Materially changing the wallet pool** (e.g., raising `copy_active_list_target`) shifts
  which signals fire — note it in `audit_log` as a pool-expansion marker so before/after
  data isn't conflated.

If you're unsure whether a change resets the window: assume it does, and ask.

### Paper → shadow → live (the money switches)

Real money is gated behind explicit flags, currently **off**:
- `COPY_LIVE_ENABLED` — enables the real-swap signing path (shadow).
- `COPY_LIVE_FULL_ENABLED` — full-size real swaps.
- `COPY_SOLANA_PRIVATE_KEY` — the funded wallet's secret (empty now).
- *(STRUCTURE's `HYPERLIQUID_AGENT_PRIVATE_KEY` is moot — STRUCTURE is decommissioned.)*

**Do not flip these casually.** The go-live procedure (only after the data justifies it and
Roy decides) is roughly: generate/fund a wallet → set the key in `.env` →
`COPY_LIVE_ENABLED=true` with `COPY_LIVE_FULL_ENABLED=false` (shadow only) → watch
calibration → only then `COPY_LIVE_FULL_ENABLED=true`. Each step is deliberate and
monitored.

### The trust/safety guardrails already in the code (don't remove)

- **Rug check before closing a paper trade** — if Birdeye liquidity has collapsed, the bot
  books a ~total loss instead of a fictitious profit at a stale price.
- **Price-scale anomaly guard** (`trailing_stop.py`) — skips a management cycle if the
  price ratio is absurd (>10,000×), catching oracle/decimals glitches.
- **Slippage floor** — paper fills assume a minimum realistic slippage (150 bps) so
  results aren't rosy.
- **Vetting-before-ingest** — wallets are validated (base58, vetted source) before they can
  drive trades; discovery output is bounded per run.
- **kill-criteria = alerts only** — strategy failure warns a human; it does not auto-trade
  or auto-promote.
- **Roster reloads happen at startup only** — the conviction roster (and the team-follow
  roster) are read when `bot_copy` starts. After changing a roster you **must**
  `docker compose restart bot_copy`, or the change won't take effect.

### Process for any change

1. Make it on a branch or commit to `main` only when ready (push = deploy in ~60s).
2. Run the relevant tests (`python -m pytest tests/...`). The wallet-pool logic is well
   covered — keep it that way.
3. If it touches locked config, write the `audit_log` justification **first**.
4. After deploy, verify: `docker compose ps`, logs, the relevant Discord/Grafana signal.
5. Never skip git hooks or commit secrets.

### Going live with the manual edits to this doc

Remember the Access Sheet warning: when COPY eventually trades real money, the wallet
discovery → auto-commit-to-`main` pipeline should gain a **human review gate** (commit to a
branch, merge after a glance) instead of auto-pushing — untrusted page content feeding a
real-money bot needs a person in the loop.
