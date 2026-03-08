#!/usr/bin/env bash
# 01_server_setup.sh — VPS provisioning for Who Owns Atlanta
#
# Run as root on woa-1 (Ubuntu 24.04 LTS):
#   ssh woa-1 'bash -s' < scripts/deploy/01_server_setup.sh
#
# Prerequisites on VPS before running:
#   /root/.cloudflare.ini  (dns_cloudflare_api_token = ...)
#
set -euo pipefail

REPO_URL="https://github.com/Who-Owns-Atlanta/who-owns-atlanta"
DOMAIN="who-owns-atlanta.org"
EMAIL="info@who-owns-atlanta.org"
WEBROOT="/var/www/who-owns-atlanta"
DEPLOY_HOME="/home/deploy/who-owns-atlanta"

# ---------------------------------------------------------------------------
# deploy user
# ---------------------------------------------------------------------------
if ! id deploy &>/dev/null; then
    useradd -m -s /bin/bash deploy
fi
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

# ---------------------------------------------------------------------------
# packages
# ---------------------------------------------------------------------------
apt-get update -qq

# Docker (official repo) — must install before adding deploy to docker group
if ! command -v docker &>/dev/null; then
    apt-get install -y ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

usermod -aG docker,sudo deploy

apt-get install -y nginx python3-certbot-dns-cloudflare ufw

# uv
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# netdata
if ! command -v netdata &>/dev/null; then
    curl -fsSL https://my-netdata.io/kickstart.sh | bash -s -- --non-interactive
fi

# ---------------------------------------------------------------------------
# ufw
# ---------------------------------------------------------------------------
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ---------------------------------------------------------------------------
# nginx global config additions
# ---------------------------------------------------------------------------
NGINX_CONF="/etc/nginx/nginx.conf"

if ! grep -q "woa_api" "$NGINX_CONF"; then
    # Insert before closing } of http block
    sed -i '/^\s*include \/etc\/nginx\/sites-enabled/i\
\
    limit_req_zone $binary_remote_addr zone=woa_api:10m rate=10r/s;\
    proxy_cache_path /var/cache/nginx/woa levels=1:2 keys_zone=woa_cache:10m\
                     max_size=100m inactive=1d use_temp_path=off;\
' "$NGINX_CONF"
fi

mkdir -p /var/cache/nginx/woa

# ---------------------------------------------------------------------------
# web directories
# ---------------------------------------------------------------------------
for d in owner tiles leaderboard l img agent agents addresses numbers; do
    mkdir -p "$WEBROOT/$d"
done
chown -R deploy:deploy "$WEBROOT"

# ---------------------------------------------------------------------------
# clone repo as deploy user
# ---------------------------------------------------------------------------
if [ ! -d "$DEPLOY_HOME/.git" ]; then
    sudo -u deploy git clone "$REPO_URL" "$DEPLOY_HOME"
fi

# ---------------------------------------------------------------------------
# nginx site
# ---------------------------------------------------------------------------
# frontend/ lives in git; symlink it under webroot so nginx root works
ln -sfn "$DEPLOY_HOME/web/frontend" "$WEBROOT/frontend"
# nginx (www-data) must be able to traverse /home/deploy to follow the symlink
chmod o+x /home/deploy

cp "$DEPLOY_HOME/web/nginx/who-owns-atlanta.conf" /etc/nginx/sites-available/
ln -sfn /etc/nginx/sites-available/who-owns-atlanta.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx || systemctl start nginx

# ---------------------------------------------------------------------------
# TLS cert via DNS-01 challenge (no HTTP traffic needed)
# ---------------------------------------------------------------------------
chmod 600 /root/.cloudflare.ini

certbot certonly --dns-cloudflare \
    --dns-cloudflare-credentials /root/.cloudflare.ini \
    --email "$EMAIL" \
    --agree-tos \
    --non-interactive \
    -d "$DOMAIN"

nginx -t && systemctl reload nginx

# ---------------------------------------------------------------------------
# start containers
# ---------------------------------------------------------------------------
cd "$DEPLOY_HOME"
sudo -u deploy docker compose \
    -f docker-compose.yml \
    -f docker-compose.prod.yml \
    up -d

echo ""
echo "=== Phase 1 complete ==="
echo "Test by adding to /etc/hosts on dev:"
echo "  216.106.177.76  $DOMAIN"
echo "Then: curl -s https://$DOMAIN/api/health"
