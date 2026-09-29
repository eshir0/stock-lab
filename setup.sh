#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
command -v docker >/dev/null || { echo 'Docker is required.'; exit 1; }
docker compose version >/dev/null
if ! command -v python3 >/dev/null; then
  echo 'Install python3 first: apt install -y python3'
  exit 1
fi
if [ ! -f .env ]; then
  python3 - <<'PY'
import pathlib,secrets
root=pathlib.Path('.')
text=(root/'.env.example').read_text()
for key in ('APP_PASSWORD','SESSION_SECRET','POSTGRES_PASSWORD'):
    text=text.replace(key+'=\n',key+'='+secrets.token_urlsafe(32)+'\n',1)
(root/'.env').write_text(text)
PY
fi
chmod 600 .env
docker compose up -d --build --wait --wait-timeout 180
python3 - <<'PY'
from pathlib import Path
values=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
print('\nStock Lab is ready: http://'+values['APP_HOST']+':8080')
print('App password (keep private): '+values['APP_PASSWORD'])
print('Initial mode: '+values['MARKET_MODE'])
PY
