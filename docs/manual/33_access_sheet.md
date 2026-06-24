## Access Sheet (PRINT THIS — fill by hand)

_Last reviewed: 2026-06-24_

> **This page is a blank template.** It contains **no passwords** and never should — it is
> in git. **Print it, then write the credentials in by hand** and store it with your family
> password sheets, somewhere safe and offline. Whoever maintains the project needs this
> sheet plus the printed `.env` (see bottom).

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
| 9 | **Twilio** | Emergency SMS | console.twilio.com | __________ | __________ | __________ | ☐ | |
| 10 | **Cloudflare** | Remote access tunnel | one.dash.cloudflare.com | __________ | __________ | __________ | ☐ | |
| 11 | **Microsoft / OneDrive** | Vetting result files | onedrive.com | __________ | __________ | __________ | ☐ | files: `OneDrive\Documents\Claude\` |
| 12 | **Claude (Max)** | Runs wallet discovery | claude.ai | __________ | __________ | __________ | ☐ | Max subscription |
| 13 | **Email (for reports)** | Daily report email / SMTP | __________ | __________ | __________ | __________ | ☐ | |
| 14 | **Recovery codes** | 2FA backups for the above | (store with this sheet) | | | | | one-time codes |

### The live `.env` (the master key file)

The server's `~/crypto-fleet/.env` holds **every API key and the trading-wallet secret**. It
is intentionally **not** in git and **cannot be regenerated** if lost. To keep a physical
backup:

```bash
ssh fleet hetzner.com 'cat ~/crypto-fleet/.env'    # then print/save the output offline
```

Store the printed `.env` **with this Access Sheet**, securely. Treat it like the keys to a
safe — anyone with it can move money once live trading is on.

### Who can use the emergency stop

`/panic` only works for authorized Discord/Telegram users (by user ID). **Make sure at
least one trusted person besides Roy is configured** (`DISCORD_OWNER_USER_ID` /
`TELEGRAM_OWNER_USER_ID`). Record who: ____________________________________________
