#!/usr/bin/env bash
# Ubuntu/Debian + systemd. Existing config/data survive upgrades.
set -euo pipefail
umask 022
if [[ ${1:-} == --help ]]; then
  echo 'Usage: sudo bash deploy/server/install.sh [--upgrade]'
  echo 'Requires python3, python3-venv, python3-pip and systemd.'
  exit 0
fi
if [[ $# -gt 1 || ( $# -eq 1 && $1 != --upgrade ) ]]; then echo 'Unknown option' >&2; exit 2; fi
[[ $EUID -eq 0 ]] || { echo 'Run as root (sudo).' >&2; exit 1; }
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || { echo 'Linux with systemd is required.' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
command -v systemctl >/dev/null
command -v curl >/dev/null
BUNDLE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
INSTALL_ROOT=/opt/paperflow-web
DATA_ROOT=/var/lib/paperflow
ENV_FILE=/etc/paperflow-web.env
# Do not overwrite an unrelated or manually managed deployment.
if [[ -e $INSTALL_ROOT && ! -f $INSTALL_ROOT/.portable-install ]]; then
  echo "$INSTALL_ROOT already exists without the portable-install marker. See README migration instructions." >&2
  exit 1
fi
if [[ -e $INSTALL_ROOT/.portable-install && ${1:-} != --upgrade ]]; then
  echo 'Already installed. Use --upgrade; config and data will be preserved.' >&2
  exit 1
fi
getent group paperflow >/dev/null || groupadd --system paperflow
id paperflow >/dev/null 2>&1 || useradd --system --gid paperflow --home-dir "$DATA_ROOT" --shell /usr/sbin/nologin paperflow
install -d -m 0755 "$INSTALL_ROOT"
install -d -o paperflow -g paperflow -m 0750 "$DATA_ROOT"
if [[ ! -f $ENV_FILE ]]; then
  install -o root -g paperflow -m 0640 "$BUNDLE_ROOT/deploy/server/paperflow.env.example" "$ENV_FILE"
  python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import secrets,sys
p=Path(sys.argv[1]);s=p.read_text()
s=s.replace('PAPERFLOW_WEB_PASSWORD=CHANGE_ME', 'PAPERFLOW_WEB_PASSWORD='+secrets.token_urlsafe(24))
s=s.replace('PAPERFLOW_WEB_SECRET=CHANGE_ME', 'PAPERFLOW_WEB_SECRET='+secrets.token_hex(32))
p.write_text(s)
PY
fi
# Build and install before switching services. A failure leaves the previous
# environment available, and the new venv never changes path after creation.
ENV_ID=$(date -u +%Y%m%d%H%M%S)-$$
NEW_ENV=$INSTALL_ROOT/venvs/$ENV_ID
python3 -m venv "$NEW_ENV"
"$NEW_ENV/bin/python" -m pip install --upgrade pip
"$NEW_ENV/bin/python" -m pip install "$BUNDLE_ROOT"
"$NEW_ENV/bin/python" -m pip check
"$NEW_ENV/bin/python" -c 'import paperflow.web.app, paperflow.web.worker; print("Imports OK")'
OLD_ENV=$(readlink -f "$INSTALL_ROOT/.venv" 2>/dev/null || true)
# Stop at the final switch; the worker can resume interrupted work.
systemctl stop paperflow-worker.service paperflow-web.service 2>/dev/null || true
ln -s "$NEW_ENV" "$INSTALL_ROOT/.venv.next"
mv -Tf "$INSTALL_ROOT/.venv.next" "$INSTALL_ROOT/.venv"
install -m 0644 "$BUNDLE_ROOT/deploy/server/paperflow-web.service" /etc/systemd/system/paperflow-web.service
install -m 0644 "$BUNDLE_ROOT/deploy/server/paperflow-worker.service" /etc/systemd/system/paperflow-worker.service
printf '%s\n' "$ENV_ID" > "$INSTALL_ROOT/.portable-install"
systemctl daemon-reload
systemctl enable --now paperflow-web.service paperflow-worker.service
ok=0
for attempt in {1..15}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8765/healthz >/dev/null && systemctl is-active --quiet paperflow-worker.service; then ok=1; break; fi
  sleep 1
done
if [[ $ok -ne 1 ]]; then
  if [[ -n $OLD_ENV && -d $OLD_ENV ]]; then
    ln -s "$OLD_ENV" "$INSTALL_ROOT/.venv.rollback"
    mv -Tf "$INSTALL_ROOT/.venv.rollback" "$INSTALL_ROOT/.venv"
    systemctl restart paperflow-web.service paperflow-worker.service
    echo 'Health check failed; restored previous environment.' >&2
  fi
  echo 'Inspect journalctl -u paperflow-web -u paperflow-worker.' >&2
  exit 1
fi
printf '\nInstalled. Credentials are in %s (PAPERFLOW_WEB_USERNAME/PASSWORD).\n' "$ENV_FILE"
echo 'Configure HTTPS reverse proxy using deploy/server/Caddyfile.example.'
if [[ -n $OLD_ENV ]]; then echo "Previous environment retained for rollback: $OLD_ENV"; fi
