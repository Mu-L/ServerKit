# ServerKit Installation Guide

This guide covers installing ServerKit on Linux. The one-line installer
(`install.sh`) supports Ubuntu 22.04+, Debian 12+, Fedora, and the RHEL family
(RHEL/Rocky/AlmaLinux/CentOS 9+); on RHEL-family hosts it also enables EPEL and
configures SELinux for the nginx reverse proxy. The manual steps below use
Ubuntu/Debian (`apt`) as the primary example — on Fedora/RHEL substitute `dnf`
and the docker-ce RHEL repo.

To connect additional servers to this panel, install the agent — see the
[serverkit-agent README](https://github.com/jhd3197/serverkit-agent/blob/main/README.md)
and [pairing.md](pairing.md). ServerKit is a modern server management panel for managing web applications, databases, Docker containers, and more.

## One-Line Install (Recommended)

```bash
curl -fsSL https://serverkit.ai/install.sh | sudo bash
```

The installer provisions Python, Docker, Nginx, SSL (best-effort), and systemd
services automatically. It uses an atomic blue/green layout: `/opt/serverkit` is
a symlink to either `/opt/serverkit-a` or `/opt/serverkit-b`, so failed updates
can roll back instantly.

### Install profiles

The installer measures the machine and asks how much to install. Answer within
15 seconds or accept the suggestion; a piped (`curl | bash`) install takes the
suggestion immediately.

| Profile | Provisions | Suited for |
|---------|-----------|------------|
| `minimal` | Panel, nginx, SQLite. **No Docker.** | 512MB–1GB boxes, LXC/OpenVZ guests where Docker cannot run, or hosts where Docker is managed elsewhere. Monitoring, domains, certificates, cron and DNS all work. |
| `standard` | Adds Docker and the compose plugin | 2GB RAM and up. This is the default. |
| `full` | Adds `fail2ban` (powers the panel's jail management) and `certbot` (automatic HTTPS) | 4GB RAM, 4 cores, 20GB disk and up. |

**A profile is a starting point, not a licence tier.** Nothing is permanently
locked: anything a profile skips can be installed later from the panel, and the
panel probes what is actually available at runtime rather than trusting the
recorded profile.

On a `minimal` install:

- Container-dependent pages (Containers, Services, Deployments) explain that
  Docker is missing and how to add it, instead of failing on first use.
- `sudo serverkit update` does **not** require Docker. Install Docker later and
  updates start validating it again automatically.
- The health doctor does not report the absent Docker service as a failure.

### Install options

| Variable | Purpose |
|----------|---------|
| `PANEL_DOMAIN=panel.example.com` | Set the panel domain and attempt Let's Encrypt |
| `SERVERKIT_PROFILE=minimal\|standard\|full` | Pick the install profile and skip the prompt |
| `SERVERKIT_PROFILE_TIMEOUT=15` | Seconds to wait at the profile prompt before taking the suggestion |
| `SERVERKIT_SKIP_SSL=1` | Skip HTTPS/certbot entirely |
| `SERVERKIT_EXTERNAL_PROXY=1` | You run your own TLS-terminating reverse proxy in front. Selects the plain-HTTP nginx site (no redirect for the proxy to loop on), skips certbot, and sets `TRUST_PROXY_HEADERS=true` + `TRUSTED_PROXY_HOPS=2`. See [Running behind your own reverse proxy](#running-behind-your-own-reverse-proxy) |
| `SERVERKIT_PUBLIC_URL=https://panel.example.com` | The public URL browsers use. Required with `SERVERKIT_EXTERNAL_PROXY` — without it websocket connections are rejected on origin |
| `SERVERKIT_CONFIG=/path/install.conf` | Read the options above from a `KEY=VALUE` file instead of the command line. Explicit environment still wins |
| `SERVERKIT_BIND_HOST=0.0.0.0` | Expose the raw backend port instead of binding it to loopback. ⚠️ Also disables trusted client IPs — with the port directly reachable, `X-Forwarded-For` is client-forgeable. Firewall it yourself |
| `INSTALL_FROM_RELEASE=1` | Install from the latest GitHub release tarball instead of cloning source |
| `SERVERKIT_VERSION=v1.7.0` | Pin a specific release version |
| `SERVERKIT_OFFLINE_TARBALL=/path/to/...tar.gz` | Use a local tarball instead of downloading |
| `SERVERKIT_MIRROR_URL=https://mirror.example.com/releases` | Fetch releases/checksums from a private mirror |

Example with a domain:

```bash
curl -fsSL https://serverkit.ai/install.sh | sudo PANEL_DOMAIN=panel.example.com bash
```

Example on a small VPS, skipping Docker:

```bash
curl -fsSL https://serverkit.ai/install.sh | sudo SERVERKIT_PROFILE=minimal bash
```

Example offline install:

```bash
curl -fsSL https://serverkit.ai/install.sh | \
  sudo SERVERKIT_OFFLINE_TARBALL=/tmp/serverkit-v1.7.0-linux-amd64.tar.gz bash
```

Example behind your own Caddy/Traefik:

```bash
curl -fsSL https://serverkit.ai/install.sh | \
  sudo SERVERKIT_EXTERNAL_PROXY=1 SERVERKIT_PUBLIC_URL=https://panel.example.com bash
```

Example re-runnable install from a config file:

```bash
cat > /root/serverkit.conf <<'EOF'
SERVERKIT_PROFILE=standard
SERVERKIT_EXTERNAL_PROXY=1
SERVERKIT_PUBLIC_URL=https://panel.example.com
EOF

curl -fsSL https://serverkit.ai/install.sh | \
  sudo SERVERKIT_CONFIG=/root/serverkit.conf bash
```

## Updating ServerKit

```bash
sudo serverkit update
```

The updater runs pre-flight checks, backs up the database, deploys into the
inactive blue/green slot, runs `flask db upgrade`, switches the symlink
atomically, and performs a health check. If the health check fails, it rolls
back to the previous slot automatically.

### Update options

```bash
sudo serverkit update --dry-run          # preview changes without applying
sudo serverkit update --force            # force update even if already current
sudo serverkit update --branch dev       # update from a git branch
sudo serverkit update --release          # update to the latest release
sudo serverkit update --release v1.7.0   # pin a release
```

Offline and mirror updates are also supported:

```bash
sudo SERVERKIT_OFFLINE_TARBALL=/tmp/serverkit-v1.7.0-linux-amd64.tar.gz \
  serverkit update --release

sudo SERVERKIT_MIRROR_URL=https://mirror.example.com/releases serverkit update
```

## Table of Contents

- [Requirements](#requirements)
- [Quick Install (Docker)](#quick-install-docker)
- [Manual Installation](#manual-installation)
- [Running behind your own reverse proxy](#running-behind-your-own-reverse-proxy)
- [Post-Installation Setup](#post-installation-setup)
- [Security Configuration](#security-configuration)
- [Notification Setup](#notification-setup)
- [Troubleshooting](#troubleshooting)

---

## Requirements

### Minimum System Requirements

- **OS** (64-bit): Ubuntu 22.04+ / Debian 12+ / Fedora / RHEL / Rocky / AlmaLinux 9+
- **CPU**: 1 vCPU (2+ recommended)
- **RAM**: 1 GB minimum (2+ GB recommended)
- **Disk**: 10 GB free space
- **Network**: Public IP with ports 80, 443, and 5000 accessible

### Software Requirements

For Docker installation:
- Docker Engine 24.0+
- Docker Compose v2.0+

For manual installation:
- Python 3.11+
- Node.js 20+
- Nginx (optional, for reverse proxy)

---

## Quick Install (Docker)

`docker compose up -d` runs ServerKit as a **single container**: gunicorn serves
the API, the React SPA and the Socket.IO websockets together on port 5000. There
is no nginx inside it.

> ⚠️ **A containerised panel cannot manage its own host.** It has no systemd, no
> host nginx and no host package manager, so managed sites, per-app vhosts and
> host services are unavailable. Use it for evaluation, for driving *other*
> servers through agents, or as a panel behind your own reverse proxy. To manage
> **this** machine, use the [one-line host install](#one-line-install-recommended)
> instead.

### Step 1: Install Docker

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install prerequisites
sudo apt install -y ca-certificates curl gnupg

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to docker group (logout/login required)
sudo usermod -aG docker $USER

# Verify installation
docker --version
docker compose version
```

### Step 2: Clone ServerKit

```bash
# Clone the repository
git clone https://github.com/jhd3197/ServerKit.git
cd ServerKit
```

### Step 3: Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Generate secure keys
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
echo "JWT_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env

# Edit configuration
nano .env
```

**Important**: Update these values in `.env`:
- `SECRET_KEY` - Unique random string for Flask sessions
- `JWT_SECRET_KEY` - Unique random string for JWT tokens
- `CORS_ORIGINS` - Your domain (e.g., `https://panel.yourdomain.com`)
- `SERVERKIT_HTTP_PORT` - Optional; host port to publish (default `5000`)

Leave the database alone unless you want PostgreSQL: compose keeps SQLite on the
`serverkit-data` volume. To use another database under Docker, set
`SERVERKIT_DATABASE_URL` — **not** `DATABASE_URL`, which compose overrides so a
relative SQLite path cannot silently place your data outside the volume.

### Step 4: Start ServerKit

```bash
# Build and start containers
docker compose up -d

# View logs
docker compose logs -f

# Check status
docker compose ps
```

### Step 5: Access ServerKit

Open your browser and navigate to `http://your-server-ip:5000` (override the
published port with `SERVERKIT_HTTP_PORT` in `.env`).

Create your admin account on first visit. The setup page asks for the
first-run setup code, which the container logs at startup
(`docker logs serverkit | grep 'setup code'`) and prints on demand:
`docker exec serverkit python cli.py setup-code`.

The container speaks plain HTTP only — it has no certificates and no HTTPS
listener. For TLS, terminate it in a reverse proxy in front of the container:
see [Running behind your own reverse proxy](#running-behind-your-own-reverse-proxy).

Your data (the SQLite database) lives on the `serverkit-data` named volume, so
`docker compose down` keeps it and `docker compose down -v` destroys it.

---

## Manual Installation

For installations without Docker.

### Step 1: Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip

# Install Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install build tools and libraries
sudo apt install -y build-essential libffi-dev libssl-dev libpq-dev

# Install Nginx (for reverse proxy)
sudo apt install -y nginx

# Install ClamAV (for malware scanning)
sudo apt install -y clamav clamav-daemon clamav-freshclam

# Install other useful tools
sudo apt install -y git curl wget
```

### Step 2: Clone and Setup Backend

```bash
# Clone repository
cd /opt
sudo git clone https://github.com/jhd3197/ServerKit.git
sudo chown -R $USER:$USER ServerKit
cd ServerKit

# Create Python virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
cd backend
pip install -r requirements.txt
pip install gunicorn gevent gevent-websocket
```

### Step 3: Build Frontend

```bash
# Navigate to frontend directory
cd /opt/ServerKit/frontend

# Install Node dependencies
npm ci

# Build production bundle
npm run build
```

### Step 4: Configure Environment

```bash
# Create environment file
cd /opt/ServerKit/backend
cp ../.env.example .env

# Generate and set secure keys
SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

sed -i "s/SECRET_KEY=.*/SECRET_KEY=$SECRET/" .env
sed -i "s/JWT_SECRET_KEY=.*/JWT_SECRET_KEY=$JWT_SECRET/" .env

# Edit other settings
nano .env
```

### Step 5: Create ServerKit Directories

```bash
# Create required directories
sudo mkdir -p /etc/serverkit
sudo mkdir -p /var/log/serverkit
sudo mkdir -p /var/quarantine

# Set permissions
sudo chown -R $USER:$USER /etc/serverkit
sudo chown -R $USER:$USER /var/log/serverkit
sudo chown -R $USER:$USER /var/quarantine
```

### Step 6: Create Systemd Service

```bash
sudo nano /etc/systemd/system/serverkit.service
```

Add the following content:

```ini
[Unit]
Description=ServerKit Server Management Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ServerKit/backend
Environment="PATH=/opt/ServerKit/venv/bin"
Environment="FLASK_ENV=production"
ExecStart=/opt/ServerKit/venv/bin/gunicorn \
    --workers 1 \
    --threads 100 \
    --bind 127.0.0.1:5000 \
    --timeout 120 \
    --access-logfile /var/log/serverkit/access.log \
    --error-logfile /var/log/serverkit/error.log \
    run:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **Keep `--workers 1` and do not set `--worker-class`.** The agent gateway holds
> all connected-agent state in memory in one process, so extra workers silently
> misroute agent commands. The app runs `async_mode='threading'`, where WebSocket
> is served by `simple-websocket`; adding the gevent-websocket worker class makes
> it double-answer the upgrade handshake, which browsers report as
> "Invalid frame header." Scale with `--threads`, not `--workers`.

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable serverkit
sudo systemctl start serverkit
sudo systemctl status serverkit
```

### Step 7: Configure Nginx Reverse Proxy

```bash
sudo nano /etc/nginx/sites-available/serverkit
```

Add the following configuration:

```nginx
server {
    listen 80;
    server_name your-domain.com;  # Change to your domain or IP

    # Frontend (static files)
    location / {
        root /opt/ServerKit/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    # Backend API
    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # WebSocket support
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/serverkit /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Step 8: Setup SSL with Let's Encrypt (Recommended)

```bash
# Install Certbot
sudo apt install -y certbot python3-certbot-nginx

# Obtain SSL certificate
sudo certbot --nginx -d your-domain.com

# Auto-renewal is configured automatically
sudo systemctl status certbot.timer
```

---

## Running behind your own reverse proxy

Already running Caddy, Traefik, Nginx Proxy Manager or your own nginx — with
Let's Encrypt, an internal PKI, or a private CA such as smallstep? ServerKit
supports that. The panel itself is CA-agnostic: certificates are entirely your
proxy's business, and ServerKit never sees them.

Two things make it work, and one thing usually breaks first.

### The redirect loop

A **host install** puts its own nginx on port 80, and the default site 301s
every plain-HTTP request to HTTPS. An external proxy terminates TLS and then
speaks plain HTTP upstream — so it receives that 301, follows it, gets another,
and the browser gives up with *"too many redirects"*. The fix is to stop that
redirect (topology A below) or to bypass that nginx entirely (topology B).

Nothing in the Flask app ever redirects to HTTPS, so once nginx is out of the
way the loop cannot recur.

### Pick a topology

| | **A — proxy → ServerKit nginx** | **B — proxy → the app directly** |
|---|---|---|
| Install type | Host install (`install.sh`) | Docker, or a host install you front yourself |
| Upstream | `127.0.0.1:80` | `127.0.0.1:5000` |
| Panel UI / API / websockets | ✅ | ✅ |
| Managed site vhosts, per-app domains | ✅ | ❌ **panel only** |
| `TRUSTED_PROXY_HOPS` | `2` | `1` |

Topology B is the only option for the Docker image — it contains no nginx.
Choose A on a host install if ServerKit also hosts your sites, because those
per-app vhosts live in the nginx you would be bypassing.

### The two environment variables

Both go in `.env` (host install: `/opt/serverkit/.env`) and both matter
regardless of topology.

| Variable | Why |
|---|---|
| `SERVERKIT_PUBLIC_URL` | **Required.** The browser's `Origin` on the Socket.IO handshake is your public HTTPS URL, which the panel does not otherwise know. Without it every realtime connection is rejected with HTTP 400 and the UI silently stops updating. Set it to the exact scheme+host you browse to, no trailing slash. |
| `TRUST_PROXY_HEADERS` | Set `true` so the real client IP is read from `X-Forwarded-For` instead of every request appearing to come from your proxy. Rate limits, login lockout and audit logs all depend on it. ⚠️ Leave it **off** if the port is exposed directly to clients — the header is client-forgeable without a proxy in front to overwrite it. |

```bash
SERVERKIT_PUBLIC_URL=https://serverkit.example.com
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_HOPS=1        # 2 for topology A — see the table above
```

`TRUSTED_PROXY_HOPS` is the number of proxies that append to `X-Forwarded-For`
before Flask sees it. Count them: in topology A your proxy appends the client,
then ServerKit's nginx appends your proxy, so it is `2`. Too low and you log
your own proxy's IP; too high and a client can forge the value.

### Topology A — keep ServerKit's nginx, drop the redirect

The plain-HTTP site ships with every install; it just isn't the default.

```bash
# 1. Serve the panel over plain HTTP, with no redirect for your proxy to loop on
sudo ln -sf /etc/nginx/sites-available/serverkit-insecure.conf \
            /etc/nginx/sites-enabled/serverkit.conf

# 2. Record that this box does not terminate TLS itself
echo insecure | sudo tee /etc/serverkit/ssl-mode
sudo sed -i 's/^SERVERKIT_SSL_MODE=.*/SERVERKIT_SSL_MODE=insecure/' /opt/serverkit/.env

# 3. Add the two variables from above to /opt/serverkit/.env, then apply
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart serverkit
```

> Run these **on the host**, not inside a container — `serverkit-insecure.conf`
> is written to the host filesystem by `install.sh` and does not exist in any
> ServerKit image.

Step 2 is not cosmetic: it is what suppresses the panel's
`Strict-Transport-Security` header. Left as `secure`, the panel tells browsers
to force HTTPS on a port that only speaks HTTP. **Both** places must change —
the panel reads the `SERVERKIT_SSL_MODE` environment variable first and only
falls back to `/etc/serverkit/ssl-mode`, so editing the file alone has no effect
on a host install, where `install.sh` wrote that variable into `.env`.

To start a **new** install already in this shape, skip the manual steps:

```bash
curl -fsSL https://serverkit.ai/install.sh -o install.sh
sudo SERVERKIT_EXTERNAL_PROXY=1 \
     SERVERKIT_PUBLIC_URL=https://serverkit.example.com \
     bash install.sh
```

`SERVERKIT_EXTERNAL_PROXY=1` selects the plain-HTTP site, skips certbot, and
writes `TRUST_PROXY_HEADERS=true` + `TRUSTED_PROXY_HOPS=2` into `.env` for you.
See [Install options](#install-options).

### Topology B — point your proxy at the app

Docker needs nothing but the environment — the shipped `docker-compose.yml`
already publishes the panel on 5000:

```bash
# .env
SERVERKIT_PUBLIC_URL=https://serverkit.example.com
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_HOPS=1
```

```bash
docker compose up -d
```

The container publishes on all interfaces by default, so port 5000 is reachable
directly and bypasses your proxy (and its TLS). If the proxy runs on the same
host, bind the panel to loopback with a `docker-compose.override.yml`:

```yaml
services:
  serverkit:
    ports: !override
      - "127.0.0.1:5000:5000"
```

Otherwise, block 5000 at the firewall.

On a host install, point your proxy at `127.0.0.1:5000` and leave ServerKit's
nginx alone (or disable it if it fights for port 80). Gunicorn serves the SPA,
the API and the websockets from that single port. Remember this is panel-only:
managed sites do not exist without the vhost layer.

### Proxy configuration

Substitute `UPSTREAM` with `localhost:80` (topology A) or `localhost:5000`
(topology B). Websockets need no special handling in Caddy or Traefik; nginx and
Apache need the upgrade headers spelled out.

**Caddy** — including a private ACME CA such as smallstep:

```caddy
{
    # Only if you use an internal CA. Omit both lines for Let's Encrypt.
    acme_ca      https://ca.lab/acme/acme/directory
    acme_ca_root /etc/caddy/root_ca.crt
}

serverkit.example.com {
    reverse_proxy UPSTREAM
}
```

**Traefik** (dynamic file provider):

```yaml
http:
  routers:
    serverkit:
      rule: "Host(`serverkit.example.com`)"
      service: serverkit
      tls:
        certResolver: internal
  services:
    serverkit:
      loadBalancer:
        servers:
          - url: "http://UPSTREAM"
```

**Nginx Proxy Manager**: add a Proxy Host for the domain, forward to the
upstream host/port over `http`, and enable **Websockets Support** — the panel's
realtime updates fail silently without it.

**Plain nginx** elsewhere on your network:

```nginx
server {
    listen 443 ssl;
    server_name serverkit.example.com;

    ssl_certificate     /etc/ssl/certs/serverkit.crt;
    ssl_certificate_key /etc/ssl/private/serverkit.key;

    location / {
        proxy_pass http://UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 7d;   # long-lived websockets
        proxy_send_timeout 7d;
    }
}
```

### Private and internal CAs

ServerKit needs no configuration for these — your proxy presents the
certificate, and only browsers and other clients have to trust the issuing root.
Install your root CA in the OS/browser trust store on the machines you browse
from.

One exception: if a **ServerKit agent** on another machine dials back to a panel
fronted by a private CA, that machine must also trust the root, or the agent's
TLS handshake fails. Install the root in its system trust store
(`/usr/local/share/ca-certificates/` + `update-ca-certificates` on Debian/Ubuntu,
`/etc/pki/ca-trust/source/anchors/` + `update-ca-trust` on RHEL-family).

### Troubleshooting

| Symptom | Cause |
|---|---|
| *"Too many redirects"* | Topology A step skipped — ServerKit's nginx is still 301ing to HTTPS. Check `readlink /etc/nginx/sites-enabled/serverkit.conf`. |
| Loop persists after the fix | Your browser cached an HSTS entry from an earlier HTTPS visit. Clear it (`chrome://net-internals/#hsts` → *Delete domain security policies*) and confirm `/etc/serverkit/ssl-mode` reads `insecure`. |
| UI loads, but nothing live-updates; console shows a failed `/socket.io/` request | `SERVERKIT_PUBLIC_URL` unset or not an exact match for the URL in the address bar (scheme, host, no trailing slash). Restart the panel after changing it. |
| Websockets fail only through the proxy | Upgrade headers or Websockets Support not enabled on the proxy. |
| Every audit-log entry and rate limit shows your proxy's IP | `TRUST_PROXY_HEADERS` is not `true`. |
| Client IPs are wrong but not the proxy's | `TRUSTED_PROXY_HOPS` mismatch — recount the proxies in front of Flask. |
| 502 from your proxy | Nothing is listening on the upstream. Check `serverkit status` / `docker compose ps`, and that the upstream port matches the topology. |

---

## Post-Installation Setup

### 1. Create Admin Account

1. Open ServerKit in your browser
2. Enter the **setup code** printed at the end of the install
   (`serverkit setup-code` shows it again), then create your admin account
3. The first registered user automatically becomes admin

The setup code stops anyone who reaches a freshly installed panel before you
from claiming it; it stops working once the admin exists. For unattended
installs, set `SERVERKIT_ADMIN_EMAIL` and `SERVERKIT_ADMIN_PASSWORD` (and
optionally `SERVERKIT_ADMIN_USERNAME`) when running the installer to create
the admin directly — no setup code involved.

### 2. Update ClamAV Definitions

```bash
# Update virus definitions
sudo freshclam

# Restart ClamAV daemon
sudo systemctl restart clamav-daemon
```

### 3. Configure Firewall (UFW)

```bash
# Enable UFW
sudo ufw enable

# Allow SSH (important!)
sudo ufw allow ssh

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Check status
sudo ufw status
```

---

## Security Configuration

### Enable Two-Factor Authentication

1. Go to **Settings > Security**
2. Click "Enable Two-Factor Authentication"
3. Scan the QR code with your authenticator app
4. Enter the verification code
5. **Save your backup codes securely!**

### Configure Notification Webhooks

Get alerts for security events, server issues, and more:

1. Go to **Settings > Notifications**
2. Configure your preferred channels:
   - **Discord**: Create a webhook in your server settings
   - **Slack**: Create an incoming webhook app
   - **Telegram**: Create a bot via @BotFather

### Enable File Integrity Monitoring

1. Go to **Security > File Integrity**
2. Click "Initialize Baseline" to create a snapshot
3. Periodically run "Check Integrity" to detect changes

---

## Notification Setup

### Discord Webhook

1. Open Discord and go to your server
2. Right-click a channel → Edit Channel → Integrations → Webhooks
3. Create a new webhook and copy the URL
4. In ServerKit: Settings → Notifications → Discord
5. Paste the webhook URL and enable

### Slack Webhook

1. Go to [Slack API](https://api.slack.com/apps)
2. Create a new app → Incoming Webhooks
3. Add a new webhook to your workspace
4. Copy the webhook URL
5. In ServerKit: Settings → Notifications → Slack

### Telegram Bot

1. Message @BotFather on Telegram
2. Send `/newbot` and follow instructions
3. Copy the bot token
4. Get your chat ID from @userinfobot
5. In ServerKit: Settings → Notifications → Telegram

---

## Troubleshooting

### Docker Issues

**Container won't start:**
```bash
# Check logs
docker compose logs backend

# Check if port is in use
sudo lsof -i :5000
sudo lsof -i :80
```

**Permission denied errors:**
```bash
# Fix Docker socket permissions
sudo chmod 666 /var/run/docker.sock
```

### Manual Installation Issues

**Python module not found:**
```bash
# Ensure virtual environment is activated
source /opt/ServerKit/venv/bin/activate
pip install -r requirements.txt
```

**Nginx 502 Bad Gateway:**
```bash
# Check if backend is running
sudo systemctl status serverkit

# Check backend logs
sudo tail -f /var/log/serverkit/error.log
```

**Database errors:**
```bash
# Reset database (WARNING: deletes all data)
cd /opt/ServerKit/backend
rm -f instance/serverkit.db
python -c "from app import create_app, db; app = create_app(); app.app_context().push(); db.create_all()"
```

### ClamAV Issues

**ClamAV not starting:**
```bash
# Check status
sudo systemctl status clamav-daemon

# Update definitions first
sudo systemctl stop clamav-freshclam
sudo freshclam
sudo systemctl start clamav-freshclam
sudo systemctl start clamav-daemon
```

### Getting Help

- Check logs in `/var/log/serverkit/`
- Docker logs: `docker compose logs -f`
- Open an issue on GitHub

---

## Updating ServerKit

### Docker Update

```bash
cd /path/to/ServerKit
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Manual Update

```bash
cd /opt/ServerKit
git pull

# Update backend
source venv/bin/activate
pip install -r backend/requirements.txt

# Update frontend
cd frontend
npm ci
npm run build

# Restart service
sudo systemctl restart serverkit
```

---

## Uninstalling

### Docker

```bash
cd /path/to/ServerKit
docker compose down -v  # -v removes volumes (data)
cd ..
rm -rf ServerKit
```

### Manual

```bash
sudo systemctl stop serverkit
sudo systemctl disable serverkit
sudo rm /etc/systemd/system/serverkit.service
sudo rm /etc/nginx/sites-enabled/serverkit
sudo rm /etc/nginx/sites-available/serverkit
sudo rm -rf /opt/ServerKit
sudo rm -rf /etc/serverkit
sudo rm -rf /var/log/serverkit
sudo systemctl daemon-reload
sudo systemctl reload nginx
```
