## Access Sheet (PRINT THIS — fill by hand)

_Last reviewed: 2026-06-24_

*Print in **LANDSCAPE**. The detailed instructions are on the **last page**, so the tables
stay clean to fill in.*

### Accounts & consoles

The **Account/email, Username, Password** columns are wide and blank — fill them by hand.
Tick **2FA** where enabled.

<table class="sheet">
<colgroup>
<col style="width:3%"><col style="width:11%"><col style="width:14%"><col style="width:20%"><col style="width:18%"><col style="width:20%"><col style="width:4%"><col style="width:10%">
</colgroup>
<thead><tr><th>#</th><th>Service</th><th>Console / login URL</th><th>Account / email</th><th>Username</th><th>Password</th><th>2FA</th><th>Notes</th></tr></thead>
<tbody>
<tr><td>1</td><td><b>Hetzner</b></td><td>console.hetzner.cloud</td><td></td><td></td><td></td><td>☐</td><td>+ SSH key</td></tr>
<tr><td>2</td><td><b>SSH key</b> (server)</td><td>(key file on the PC)</td><td></td><td></td><td></td><td>☐</td><td>connect: <code>ssh fleet</code> + passphrase</td></tr>
<tr><td>3</td><td><b>GitHub</b></td><td>github.com</td><td></td><td></td><td></td><td>☐</td><td>RoyTK/crypto-trading-fleet</td></tr>
<tr><td>4</td><td><b>Helius</b></td><td>dashboard.helius.dev</td><td></td><td></td><td></td><td>☐</td><td>via SSH SOCKS tunnel; bills &rarr; roytkelly@gmail.com</td></tr>
<tr><td>5</td><td><b>Cielo</b></td><td>app.cielo.finance</td><td></td><td></td><td></td><td>☐</td><td></td></tr>
<tr><td>6</td><td><b>Birdeye</b></td><td>birdeye.so</td><td></td><td></td><td></td><td>☐</td><td>keep Chrome logged in</td></tr>
<tr><td>7</td><td><b>Discord</b></td><td>discord.com</td><td></td><td></td><td></td><td>☐</td><td>#alerts + /panic</td></tr>
<tr><td>8</td><td><b>Telegram</b></td><td>t.me</td><td></td><td></td><td></td><td>☐</td><td>backup alerts</td></tr>
<tr><td>9</td><td><b>Twilio</b> &mdash; not set up</td><td>console.twilio.com</td><td></td><td></td><td></td><td>☐</td><td>no SMS (paper phase)</td></tr>
<tr><td>10</td><td><b>Privacy.com</b></td><td>privacy.com</td><td></td><td></td><td></td><td>☐</td><td>virtual cards (pays Helius etc.)</td></tr>
<tr><td>11</td><td><b>Cloudflare</b></td><td>one.dash.cloudflare.com</td><td></td><td></td><td></td><td>☐</td><td>remote-access tunnel</td></tr>
<tr><td>12</td><td><b>Microsoft / OneDrive</b></td><td>onedrive.com</td><td></td><td></td><td></td><td>☐</td><td>Documents\Claude\</td></tr>
<tr><td>13</td><td><b>Claude (Max)</b></td><td>claude.ai</td><td></td><td></td><td></td><td>☐</td><td>runs discovery</td></tr>
<tr><td>14</td><td><b>Email</b> (reports)</td><td>(SMTP provider)</td><td></td><td></td><td></td><td>☐</td><td>trading@generalaisystems.com</td></tr>
<tr><td>15</td><td><b>2FA recovery codes</b></td><td>(store with this sheet)</td><td></td><td></td><td></td><td></td><td>one-time codes</td></tr>
</tbody>
</table>

### Crypto wallets — usernames, passwords & recovery phrases

⚠️ Recovery phrases are the most sensitive items on this sheet — **read the safety note on the
last page before writing them.**

<table class="sheet">
<colgroup>
<col style="width:12%"><col style="width:11%"><col style="width:16%"><col style="width:16%"><col style="width:37%"><col style="width:8%">
</colgroup>
<thead><tr><th>Wallet</th><th>Type / chain</th><th>Username</th><th>Password</th><th>12-word recovery phrase</th><th>Notes</th></tr></thead>
<tbody>
<tr><td><b>Phantom</b></td><td>Solana</td><td></td><td></td><td></td><td>phantom.com</td></tr>
<tr><td><b>Rabby</b></td><td>EVM</td><td></td><td></td><td></td><td>rabby.io</td></tr>
<tr><td><b>MetaMask</b></td><td>EVM</td><td></td><td></td><td></td><td>metamask.io</td></tr>
<tr><td><b>Kraken</b></td><td>Exchange</td><td></td><td></td><td></td><td>kraken.com &mdash; no seed; enable 2FA</td></tr>
</tbody>
</table>

### Emergency-stop (`/panic`) authorized users

`/panic` (in Discord/Telegram) only works for authorized user IDs. **Make sure at least one
trusted person besides Roy is configured** (`DISCORD_OWNER_USER_ID` /
`TELEGRAM_OWNER_USER_ID`). Record them here:

- Person 1: ______________________________   Person 2: ______________________________

<div class="page-break"></div>

### Instructions & Notes (read before filling)

**This is a blank template.** It contains no passwords (it lives in git). Print it, write the
credentials in by hand, and store it with your family password sheets — somewhere safe and
offline. Whoever maintains the project needs this sheet **plus** the printed `.env` (below).

**Filling it in:** the wide blank columns (Account/email, Username, Password, and the wallet
recovery phrase) are yours to write in. Tick **2FA** where you have it enabled. The narrow
columns (Service, Console URL, Notes) are pre-filled reference.

> ⚠️ **A 12-word recovery phrase IS the money.** Anyone who has it can drain that wallet.
> Write the phrases by hand on this printed sheet ONLY; store them in a safe/lockbox; and
> **never** type, photograph, or store them on any computer, phone, cloud, or in this
> project. If a phrase is ever exposed, move the funds to a new wallet immediately.

**The live `.env` — the master key file.** The server's `~/crypto-fleet/.env` holds every API
key and the trading-wallet secret. It is **not** in git and **cannot be regenerated** if
lost. Print a copy to store with this sheet:

```bash
# inside your normal SSH session on the server:
cat ~/crypto-fleet/.env
# ...or one-shot from your PC (use your usual SSH target):
ssh fleet 'cat ~/crypto-fleet/.env'
```

Copy/print the output, then **delete any digital copy**. Treat the printed `.env` like the
keys to a safe — anyone with it can move money once live trading is on.
