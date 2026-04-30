# Cloudflare Tunnel — remote access for Phase 0–early live

## Why Cloudflare Tunnel (vs VPN)

Phase 0 through early live (< $50k deployed capital) we use a **Cloudflare Tunnel + auth** pattern. Trade-offs:

- **Pros**: ~5 min to set up, no VPS firewall changes, free, Cloudflare Access enforces SSO before any request hits the box, audit log of every login.
- **Cons**: routes through Cloudflare. Not appropriate once attack surface = real money.

## Trigger to switch to VPN-only

When `DEPLOYED_CAPITAL_USD >= VPN_TRIGGER_THRESHOLD_USD` (default $50,000):
1. Stand up WireGuard on the Hetzner VPS
2. Move all internal services (Grafana, pgAdmin) to bind only to the VPN interface
3. Tear down the Cloudflare Tunnel
4. Update `OPERATIONS.md`

## One-time setup (Phase 0)

Run on the Hetzner VPS, AFTER Hetzner is provisioned and you have a domain in your Cloudflare account.

```bash
# Install cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Login (opens a browser link to authorize the cert against your Cloudflare account)
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create crypto-fleet

# Note the tunnel UUID it prints — save it
# Token format you'll put in .env (Hetzner side):
#   CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

## tunnel-config.yml (lives at /etc/cloudflared/config.yml on the VPS)

```yaml
tunnel: <UUID-from-create>
credentials-file: /root/.cloudflared/<UUID>.json

ingress:
  - hostname: grafana.fleet.<your-domain>.com
    service: http://localhost:3000
  - hostname: prometheus.fleet.<your-domain>.com
    service: http://localhost:9090
  - service: http_status:404
```

## DNS records (in the Cloudflare dashboard or via cloudflared)

```bash
cloudflared tunnel route dns crypto-fleet grafana.fleet.<your-domain>.com
cloudflared tunnel route dns crypto-fleet prometheus.fleet.<your-domain>.com
```

## Cloudflare Access policy (REQUIRED — do this before going live with the tunnel)

In the Cloudflare dashboard → Zero Trust → Access → Applications:

1. Add Application → Self-hosted
2. Application domain: `grafana.fleet.<your-domain>.com` (and again for prometheus)
3. Identity provider: One-time PIN (using `trading@generalaisystems.com`) — sufficient for Phase 0
4. Policy: "Allow" for emails matching `trading@generalaisystems.com` AND `roy@generalaisystems.ai`
5. Session duration: 24h

This means: hitting `grafana.fleet.<your-domain>.com` from any browser prompts a one-time PIN to your email before Grafana even sees the request.

## Run as a systemd service

```bash
sudo cloudflared service install <CLOUDFLARE_TUNNEL_TOKEN>
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

## Health check from a non-VPN network (Phase 0 shakedown gate item #7)

From any browser (mobile data is fine):
1. Visit `grafana.fleet.<your-domain>.com`
2. Cloudflare Access prompts for email PIN
3. Enter the PIN sent to `trading@generalaisystems.com`
4. Grafana login screen loads

## Cost

- Cloudflare Tunnel: $0 (free tier covers this entirely)
- Cloudflare Access: $0 (free tier: 50 users / month; we have 1)
- Domain: $9/yr if not already owned
