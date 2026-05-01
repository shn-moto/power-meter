#!/usr/bin/env bash
set -euo pipefail

: "${CLOUDFLARE_TUNNEL_TOKEN:?Set CLOUDFLARE_TUNNEL_TOKEN before running this script}"

sudo cloudflared service install "$CLOUDFLARE_TUNNEL_TOKEN"
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager