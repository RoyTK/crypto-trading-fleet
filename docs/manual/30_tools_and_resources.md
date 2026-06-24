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
| **Helius** | Solana wallet webhooks (COPY's lifeblood) | **$49** Developer (10M credits) + autoscale $5/1M, **$149 cap = 30M** | dashboard.helius.dev | **Reach via the SSH SOCKS tunnel** (account is German-IP bound) — see below. Usage alerts + bills emailed to **roytkelly@gmail.com** | `HELIUS_API_KEY`, `HELIUS_WEBHOOK_AUTH_SECRET`, `HELIUS_RPC_URL` |
| **Cielo** | Wallet PnL/win-rate stats (curation) | $65 Pro | app.cielo.finance | Account login | `CIELO_API_KEY` |
| **Birdeye** | Token prices, liquidity, trader discovery | Free/Lite (~$0–19) | birdeye.so | Logged-in Chrome session (used by discovery) + API key | `BIRDEYE_API_KEY` |
| **Jupiter** | Solana DEX quotes/swaps | Free | jup.ag | Public API, no key | (none) |
| **Coinglass** | Liquidation data (STRUCTURE) | $0 (disabled) | coinglass.com | Disabled via `STRUCTURE_LIQ_CASCADE_ENABLED=false`; its **real-time tier (~$500/mo)** is the upgrade STRUCTURE would need to be revived — not justified in paper phase | `COINGLASS_API_KEY` |
| **Discord** | Alerts + `/panic` + `/status` | Free | discord.com | Bot in the server; owner ID authorizes `/panic` | `DISCORD_BOT_TOKEN`, `DISCORD_*_CHANNEL_ID`, `DISCORD_OWNER_USER_ID`, `COPY_DISCORD_WEBHOOK` |
| **Telegram** | Alerts + `/panic` (backup channel) | Free | t.me | Bot via BotFather; owner ID | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_USER_ID` |
| **Twilio** | P0 emergency SMS | **$0 — NOT set up** | console.twilio.com | Not configured during paper phase (SMS wasn't worth paying for); P0 reaches you via Discord + Telegram instead. Add if going live. | `TWILIO_*` (unset) |
| **Privacy.com** | Virtual cards used to **pay for some services** (e.g. Helius) | per-card | privacy.com | Roy's account; login on the Access Sheet | (none) |
| **Email (SMTP)** | Daily digest/report email | included | (provider) | Digest is sent to **`trading@generalaisystems.com`** | `SMTP_*`, `SMTP_TO` |
| **OneDrive** | Where browser discovery writes vetting results | Included (M365) | onedrive.com | Roy's account; files at `C:\Users\Roy\OneDrive\Documents\Claude\` | (none) |
| **Claude Max** | Runs the browser wallet-discovery/vetting | Included (Max sub) | claude.ai | Max subscription; the discovery uses Max quota, not an API key | (uses Max login, not `.env`) |
| **Cloudflare** | Secure remote access to the server | Free | one.dash.cloudflare.com | Tunnel token | `CLOUDFLARE_TUNNEL_TOKEN` |

Rough monthly total today: **~$140–150** (Hetzner + Helius + Cielo + Birdeye; **no SMS**).
The Helius bill is the variable one — it scales with how many/active wallets COPY watches.
Some services are paid via a **Privacy.com** virtual card rather than a card directly.

### Payments & crypto wallets (mostly for the eventual live phase)

- **Privacy.com** — virtual cards used to pay for some subscriptions (e.g. Helius). Login
  on the Access Sheet.
- **Crypto wallets / exchange** — used to hold and fund trading capital (relevant once live
  trading is on; mostly idle now): **Phantom** (Solana), **Rabby** and **MetaMask** (EVM
  chains), and **Kraken** (exchange / on-ramp). Each browser wallet has a **12-word
  recovery phrase** — the most sensitive secrets in the whole project. They live on the
  Access Sheet with extra-secure handling; **never** type or store them anywhere digital
  connected to this project.

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
