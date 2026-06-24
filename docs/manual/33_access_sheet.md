## Access Sheet (PRINT THIS — fill by hand)

_Last reviewed: 2026-06-24_

> **This page is a blank template.** It contains **no passwords** and never should — it is
> in git. **Print it, then write the credentials in by hand** and store it with your family
> password sheets, somewhere safe and offline. Whoever maintains the project needs this
> sheet plus the printed `.env` (see bottom).
>
> **Print in LANDSCAPE** so there's room to write. The build produces a ready-to-print
> landscape version of just this sheet at **`docs/ACCESS_SHEET.html`** (open in a browser →
> Print → Landscape). The full manual's `docs/MANUAL.html` is portrait.

### Accounts & consoles

| # | Service | What it unlocks | Console / login URL | Account / email | Username | Password | 2FA? | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | **Hetzner** | The server + backups | console.hetzner.cloud | __________ | __________ | __________ | ☐ | also need SSH key for the box |
| 2 | **SSH key (server)** | Logging into the server | (key file on the PC) | n/a | `fleet` | (key passphrase) ____ | ☐ | connect: `ssh fleet hetzner.com` |
| 3 | **GitHub** | Code; push = deploy | github.com | __________ | __________ | __________ | ☐ | repo: RoyTK/crypto-trading-fleet |
| 4 | **Helius** | Solana webhooks (COPY) | dashboard.helius.dev | __________ | __________ | __________ | ☐ | **open via SSH SOCKS tunnel** (see Tools) |
| 5 | **Cielo** | Wallet PnL stats | app.cielo.finance | __________ | __________ | __________ | ☐ | |
| 6 | **Birdeye** | Token data / discovery | birdeye.so | __________ | __________ | __________ | ☐ | keep Chrome logged in for discovery |
| 7 | **Discord** | Alerts + `/panic` | discord.com | __________ | __________ | __________ | ☐ | note the server + #alerts channel |
| 8 | **Telegram** | Backup alert channel | t.me | __________ | __________ | __________ | ☐ | |
| 9 | **Twilio** | Emergency SMS | console.twilio.com | _(not set up)_ | — | — | — | NOT configured (no SMS in paper phase) |
| 9b | **Privacy.com** | Pays for some services (Helius etc.) | privacy.com | __________ | __________ | __________ | ☐ | virtual cards |
| 10 | **Cloudflare** | Remote access tunnel | one.dash.cloudflare.com | __________ | __________ | __________ | ☐ | |
| 11 | **Microsoft / OneDrive** | Vetting result files | onedrive.com | __________ | __________ | __________ | ☐ | files: `OneDrive\Documents\Claude\` |
| 12 | **Claude (Max)** | Runs wallet discovery | claude.ai | __________ | __________ | __________ | ☐ | Max subscription |
| 13 | **Email (for reports)** | Daily report email / SMTP | __________ | __________ | __________ | __________ | ☐ | |
| 14 | **Recovery codes** | 2FA backups for the above | (store with this sheet) | | | | | one-time codes |

### Crypto wallets & recovery phrases (the MOST sensitive items)

> ⚠️ **A 12-word recovery phrase IS the money.** Anyone who has it can drain that wallet.
> Write these by hand on the printed sheet ONLY, store them in a safe/lockbox, and **never**
> type or photograph them or store them on any computer, phone, cloud, or in this project.
> If a phrase is ever exposed, move the funds to a new wallet immediately.

| Wallet | Type / chain | Used for | Address (public, ok to write) | 12-word recovery phrase (store securely) | Notes |
|---|---|---|---|---|---|
| **Phantom** (phantom.com) | Solana | COPY trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **Rabby** (rabby.io) | EVM (Base/Arbitrum/ETH) | EVM trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **MetaMask** | EVM | EVM trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **Kraken** (kraken.com) | Exchange | On-ramp / hold capital | (account, not a phrase) | login on the accounts table above | enable 2FA |

### The live `.env` (the master key file)

The server's `~/crypto-fleet/.env` holds **every API key and the trading-wallet secret**. It
is intentionally **not** in git and **cannot be regenerated** if lost. To keep a physical
backup:

```bash
# inside your normal SSH session on the server:
cat ~/crypto-fleet/.env
# ...or as a one-shot from your PC (use your usual SSH target):
ssh fleet 'cat ~/crypto-fleet/.env'
```

Copy/print the output, then **delete any digital copy**. Store the printed `.env` **with
this Access Sheet**, securely. Treat it like the keys to a safe — anyone with it can move
money once live trading is on.

### Who can use the emergency stop

`/panic` only works for authorized Discord/Telegram users (by user ID). **Make sure at
least one trusted person besides Roy is configured** (`DISCORD_OWNER_USER_ID` /
`TELEGRAM_OWNER_USER_ID`). Record who: ____________________________________________
