#!/usr/bin/env bash
# Deploy Grocer to the Unraid server: copies this folder over SSH, builds the image, (re)starts the container.
# Usage: ./deploy.sh [root@192.168.0.83]
set -euo pipefail
HOST="${1:-root@192.168.0.83}"
SRC=/mnt/user/appdata/grocer-src
DATA=/mnt/user/appdata/grocer
cd "$(dirname "$0")"

# First run: build .env from the template, asking for the secrets
if [[ ! -f .env ]]; then
  cp .env.example .env
  read -rsp "Mealie API token: " MT; echo
  read -rsp "Home Assistant long-lived token: " HT; echo
  sed -i "s|^MEALIE_TOKEN=.*|MEALIE_TOKEN=$MT|; s|^HA_TOKEN=.*|HA_TOKEN=$HT|" .env
  chmod 600 .env
fi

# the browser page has no password (LAN only)
sed -i "s|^VNC_PASSWORD=.*|VNC_PASSWORD=|" .env

echo "Copying to $HOST:$SRC ..."
tar --exclude=.git --exclude=.github --exclude=tests --exclude=__pycache__ --exclude=.pytest_cache -cz . | ssh "$HOST" "mkdir -p $SRC $DATA && tar -xz -C $SRC"

echo "Building image (first build installs Chrome, a few minutes) ..."
ssh "$HOST" "cd $SRC && docker build -q -t grocer:latest . && \
  (docker rm -f grocer >/dev/null 2>&1 || true) && \
  docker run -d --name grocer --restart unless-stopped --env-file .env \
    -p 8790:8790 -p 6080:6080 -v $DATA:/data --shm-size 1g grocer:latest"

echo
echo "Done. Next:"
echo "  1. Sign in to Walmart:  http://192.168.0.83:8790/signin"
echo "  2. Build the list:      http://192.168.0.83:8790/review  (tap Rebuild list)"
