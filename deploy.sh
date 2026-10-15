#!/usr/bin/env bash
# Update Grocer on the Unraid server: pull the image GitHub built and recreate the container.
# Everything that matters (database, Walmart sign-in, grocer.env with the tokens) lives in
# /mnt/user/appdata/grocer and is kept.
# Usage: ./deploy.sh [root@192.168.0.83]
#
# Unraid's Docker tab can do the same thing ("Check for updates", then "Apply update") once the
# container was created from unraid/grocer.xml.
set -euo pipefail
HOST="${1:-root@192.168.0.83}"
DATA=/mnt/user/appdata/grocer
IMAGE=ghcr.io/crimson3076/grocer:latest
cd "$(dirname "$0")"

# First time only: put the settings and tokens next to the data
if ! ssh "$HOST" "test -f $DATA/grocer.env"; then
  if [[ ! -f .env ]]; then
    cp .env.example .env
    read -rsp "Mealie API token: " MT; echo
    read -rsp "Home Assistant long-lived token: " HT; echo
    sed -i "s|^MEALIE_TOKEN=.*|MEALIE_TOKEN=$MT|; s|^HA_TOKEN=.*|HA_TOKEN=$HT|" .env
  fi
  echo "Copying .env to $HOST:$DATA/grocer.env ..."
  ssh "$HOST" "mkdir -p $DATA && umask 077 && cat > $DATA/grocer.env" < .env
fi

echo "Pulling $IMAGE and restarting ..."
ssh "$HOST" "docker pull -q $IMAGE && \
  (docker rm -f grocer >/dev/null 2>&1 || true) && \
  docker run -d --name grocer --restart unless-stopped \
    -p 8790:8790 -p 6080:6080 -v $DATA:/data --shm-size 1g $IMAGE >/dev/null && \
  docker image prune -f >/dev/null"

echo "Done: http://192.168.0.83:8790"
