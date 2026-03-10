#!/usr/bin/env bash
# Deploy script for Who Owns Atlanta
#
# Usage:
#   scripts/deploy/deploy.sh [--frontend] [--tiles] [--static] [--all]
#
# Options:
#   --frontend   git pull on VPS + Cloudflare cache purge
#   --tiles      rsync local tiles → VPS
#   --static     rsync local static pages (owner/, leaderboard/, l/, etc.) → VPS
#   --all        run all three

set -euo pipefail

REMOTE="woa-1"
REMOTE_REPO="/home/deploy/who-owns-atlanta"
REMOTE_WWW="/var/www/who-owns-atlanta"
LOCAL_WWW="/var/www/who-owns-atlanta"

CF_INI="${HOME}/.secrets/cloudflare.ini"
CF_ZONE="97461a97717b524c84215b8ac5ba5677"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

cf_purge() {
  local token
  token=$(grep dns_cloudflare_api_token "$CF_INI" | cut -d= -f2 | tr -d ' ')
  echo "==> Purging Cloudflare cache..."
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE}/purge_cache" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    --data '{"purge_everything":true}' | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('success'):
    print('    Cache purged.')
else:
    print('    ERROR:', r.get('errors'), file=sys.stderr)
    sys.exit(1)
"
}

deploy_frontend() {
  echo "==> Pulling repo on ${REMOTE}..."
  ssh "$REMOTE" "sudo -u deploy git -C ${REMOTE_REPO} pull"
  cf_purge
}

deploy_tiles() {
  echo "==> Rsyncing tiles to ${REMOTE}..."
  rsync -avz --progress --delete \
    "${LOCAL_WWW}/tiles/" \
    "${REMOTE}:${REMOTE_WWW}/tiles/"
  echo "    Tiles synced."
}

deploy_static() {
  echo "==> Rsyncing static pages to ${REMOTE}..."
  for dir in owner leaderboard l agent agents addresses numbers; do
    echo "    → ${dir}/"
    rsync -az --progress --delete \
      "${LOCAL_WWW}/${dir}/" \
      "${REMOTE}:${REMOTE_WWW}/${dir}/"
  done
  echo "    Static pages synced."
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 [--frontend] [--tiles] [--static] [--all]" >&2
  exit 1
fi

DO_FRONTEND=0
DO_TILES=0
DO_STATIC=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --frontend) DO_FRONTEND=1 ;;
    --tiles)    DO_TILES=1 ;;
    --static)   DO_STATIC=1 ;;
    --all)      DO_FRONTEND=1; DO_TILES=1; DO_STATIC=1 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

[[ $DO_FRONTEND -eq 1 ]] && deploy_frontend
[[ $DO_TILES    -eq 1 ]] && deploy_tiles
[[ $DO_STATIC   -eq 1 ]] && deploy_static

echo "==> Done."
