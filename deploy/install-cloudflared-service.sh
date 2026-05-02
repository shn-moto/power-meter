#!/usr/bin/env bash
set -euo pipefail

: "${CLOUDFLARE_TUNNEL_TOKEN:?Set CLOUDFLARE_TUNNEL_TOKEN before running this script}"

CONTAINER_NAME="power-meter-tunnel"

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d \
	--name "$CONTAINER_NAME" \
	--restart unless-stopped \
	--network host \
	cloudflare/cloudflared:latest \
	tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"

docker ps --filter "name=$CONTAINER_NAME"
docker logs --tail 50 "$CONTAINER_NAME"