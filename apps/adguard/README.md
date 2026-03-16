# AdGuard Home + Tailscale DNS

Network-wide ad blocking via Tailscale, no router access needed.

## How it works

AdGuard Home binds DNS (port 53) to your server's Tailscale IP only.
Tailscale's admin console pushes this as the DNS server to all devices on your tailnet.

## Prerequisites

- Tailscale installed on the host OS (not in a container)
- `--accept-dns=false` set on the host to prevent DNS loops

```bash
# Prevent the AdGuard host from using itself as DNS
sudo tailscale set --accept-dns=false
```

## Setup

1. Add your Tailscale IP to `.env`:

```bash
# Find your Tailscale IP
tailscale ip -4

# Add to .env
TAILSCALE_IP=100.x.x.x
```

2. Start the stack:

```bash
docker compose up -d
```

3. Complete the AdGuard setup wizard at `https://adguard.yourdomain.com`:
   - Web interface listen: `0.0.0.0:3000`
   - DNS listen: `0.0.0.0:53`
   - Configure upstream DNS (e.g. `https://dns.quad9.net/dns-query`)

4. Configure Tailscale DNS in the [admin console](https://login.tailscale.com/admin/dns):
   - Add your Tailscale IP as a **Global Nameserver**
   - Enable **Override local DNS**

## Known issues

- **Client IPs as 127.0.0.1**: Docker + Tailscale causes all tailnet clients
  to appear as localhost in AdGuard's query log. DNS filtering still works,
  but per-client stats won't be accurate.
- **Boot order**: If Docker starts before Tailscale, the container will fail
  to bind to the Tailscale IP. `restart: unless-stopped` handles this
  automatically by retrying.
