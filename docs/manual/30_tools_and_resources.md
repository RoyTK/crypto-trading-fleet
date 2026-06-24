## Tools & Resources

_Last reviewed: 2026-06-24_

Every external service the project depends on — what it's for, what it costs, where its
console is, how to get in, and the `.env` variable name(s). **Actual passwords/keys are on
the Access Sheet, never here.**

### Services

| Service | What it's for | ~Cost/mo | Console | Access notes | Env var(s) |
|---|---|---|---|---|---|
| **Hetzner** | The always-on server (and backups) | ~$25 + $4 backup | console.hetzner.cloud | Account login; SSH key for the box | (SSH key, not in `.env`) |
| **GitHub** | Source code; `git push` = deploy | Free | github.com/RoyTK/crypto-trading-fleet | SSH key with push to `main` | (SSH key) |
| **Helius** | Solana wallet webhooks (COPY's lifeblood) | **$49** Developer (10M credits) + autoscale $5/1M, **$149 cap = 30M** | dashboard.helius.dev | **Reach via the SSH SOCKS tunnel** (account is German-IP bound) — see below | `HELIUS_API_KEY`, `HELIUS_WEBHOOK_AUTH_SECRET`, `HELIUS_RPC_URL` |
| **Cielo** | Wallet PnL/win-rate stats (curation) | $65 Pro | app.cielo.finance | Account login | `CIELO_API_KEY` |
| **Birdeye** | Token prices, liquidity, trader discovery | Free/Lite (~$0–19) | birdeye.so | Logged-in Chrome session (used by discovery) + API key | `BIRDEYE_API_KEY` |
| **Jupiter** | Solana DEX quotes/swaps | Free | jup.ag | Public API, no key | (none) |
| **Coinglass** | Liquidation data (STRUCTURE) | $0 (disabled) | coinglass.com | Disabled via `STRUCTURE_LIQ_CASCADE_ENABLED=false` | `COINGLASS_API_KEY` |
| **Discord** | Alerts + `/panic` + `/status` | Free | discord.com | Bot in the server; owner ID authorizes `/panic` | `DISCORD_BOT_TOKEN`, `DISCORD_*_CHANNEL_ID`, `DISCORD_OWNER_USER_ID`, `COPY_DISCORD_WEBHOOK` |
| **Telegram** | Alerts + `/panic` (backup channel) | Free | t.me | Bot via BotFather; owner ID | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_USER_ID` |
| **Twilio** | P0 emergency SMS | ~$5 | console.twilio.com | Account; from/to numbers | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_TO_NUMBER` |
| **OneDrive** | Where browser discovery writes vetting results | Included (M365) | onedrive.com | Roy's account; files at `C:\Users\Roy\OneDrive\Documents\Claude\` | (none) |
| **Claude Max** | Runs the browser wallet-discovery/vetting | Included (Max sub) | claude.ai | Max subscription; the discovery uses Max quota, not an API key | (uses Max login, not `.env`) |
| **Cloudflare** | Secure remote access to the server | Free | one.dash.cloudflare.com | Tunnel token | `CLOUDFLARE_TUNNEL_TOKEN` |

Rough monthly total today: **~$140–150** (Hetzner + Helius + Cielo + Birdeye + small SMS).
The Helius bill is the variable one — it scales with how many/active wallets COPY watches.

### Reaching the Helius dashboard (the SOCKS tunnel)

The Helius account is tied to the server's German IP, so the dashboard must be opened
*through* the server:
1. `ssh -D 9999 -N fleet hetzner.com` (exact form; keep open).
2. Firefox → SOCKS5 proxy `127.0.0.1:9999`, "Proxy DNS when using SOCKS v5".
3. Verify at `ifconfig.me` (should show the server IP), then `dashboard.helius.dev`.
4. Turn the proxy back off when done.
For just the *subscription count* you can skip the tunnel:
`docker compose exec -T framework python -m scripts.helius_webhook_setup --list`.

### How we get vetted wallets (the discovery → trade pipeline)

```
Browser-Opus on office PC  →  vets Birdeye trending traders  →  KEEP/REJECT/TOO_FAST
   → OneDrive vetted_watch_results.txt
   → synced into repo bots/copy/vetted_watch_results.txt  (git push → deploy)
   → server: apply_vetting_results.py  → KEEP becomes ACTIVE
   → daily cron syncs active+watch to Helius webhooks
   → COPY trades on clusters of active wallets
```

**Vetting criteria (why a wallet is KEEP):** strongly positive **realized** PnL (banked,
not paper), unrealized losses not deeper than gains (not a bag-holder), **followable hold
time ~30 min–7 days** (faster = TOO_FAST, slower = swing/REJECT), multiple multi-x wins,
not a market-maker bot. A high rug-rate is *expected and good* — the edge is riding
fresh-mint pumps and exiting before they collapse. ~90% of candidates are REJECTed.
