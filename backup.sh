#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
mkdir -p backups
target="backups/stocklab-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db pg_dump -U stocklab -d stocklab -Fc > "$target"
printf 'Backup saved: %s\n' "$target"
