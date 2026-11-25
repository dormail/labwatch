#!/usr/bin/env bash
# Install (or update) labwatch as a systemd service on a Linux server.
#
#   sudo git clone https://github.com/dormail/labwatch /opt/labwatch
#   sudo /opt/labwatch/deploy/install.sh [labwatch-backup-<date>.tar.gz]
#
# Layout:
#   /opt/labwatch            code + virtualenv (root-owned, read-only for the service)
#   /var/lib/labwatch        config.yaml, .env, labwatch.sqlite (user "labwatch", mode 700)
#   /usr/local/bin/labwatch  wrapper: runs the CLI as the service user with the right config
#
# Update later with:  cd /opt/labwatch && sudo git pull && sudo deploy/install.sh
#
# Environment overrides: DATA_DIR, SERVICE_USER, PYTHON, NO_START=1 (install but don't start).
set -euo pipefail

CODE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DATA_DIR=${DATA_DIR:-/var/lib/labwatch}
SERVICE_USER=${SERVICE_USER:-labwatch}
PYTHON=${PYTHON:-python3}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
BIN_DIR=${BIN_DIR:-/usr/local/bin}
BACKUP=${1:-}

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0 $*)"
[[ -f "$CODE_DIR/pyproject.toml" ]] || die "$CODE_DIR doesn't look like a labwatch checkout"
[[ -z "$BACKUP" || -f "$BACKUP" ]] || die "backup file $BACKUP not found"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (install python3 >= 3.10, plus python3-venv on Debian/Ubuntu)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "labwatch needs Python >= 3.10, found $("$PYTHON" --version 2>&1)"

say "service user: $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "virtualenv: $CODE_DIR/.venv"
"$PYTHON" -m venv "$CODE_DIR/.venv" || die "creating the venv failed (Debian/Ubuntu: apt install python3-venv)"
"$CODE_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$CODE_DIR/.venv/bin/pip" install --quiet --upgrade "$CODE_DIR"
chmod -R a+rX "$CODE_DIR"   # service user must be able to read the code and venv

say "data directory: $DATA_DIR"
install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
if [[ -n "$BACKUP" ]]; then
  for f in config.yaml .env labwatch.sqlite; do
    if [[ -e "$DATA_DIR/$f" ]]; then
      die "$DATA_DIR/$f already exists - not overwriting it with the backup (move it away first)"
    fi
  done
  tar -xzf "$BACKUP" -C "$DATA_DIR" --no-same-owner
  say "restored $(tar -tzf "$BACKUP" | tr '\n' ' ')"
elif [[ ! -e "$DATA_DIR/config.yaml" ]]; then
  cp "$CODE_DIR/config.example.yaml" "$DATA_DIR/config.yaml"
  cp "$CODE_DIR/.env.example" "$DATA_DIR/.env"
  say "no backup given: copied the example config to $DATA_DIR - edit config.yaml and .env"
  NEW_CONFIG=1
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod -R go-rwx "$DATA_DIR"

say "CLI wrapper: $BIN_DIR/labwatch"
cat > "$BIN_DIR/labwatch" <<EOF
#!/bin/sh
# Run the labwatch CLI as the service user with the service's config.
cd "$DATA_DIR" 2>/dev/null || cd /
if [ "\$(id -un)" = "$SERVICE_USER" ]; then
  exec "$CODE_DIR/.venv/bin/labwatch" -c "$DATA_DIR/config.yaml" "\$@"
fi
exec sudo -u "$SERVICE_USER" "$CODE_DIR/.venv/bin/labwatch" -c "$DATA_DIR/config.yaml" "\$@"
EOF
chmod 755 "$BIN_DIR/labwatch"

say "systemd unit: $UNIT_DIR/labwatch.service"
sed -e "s#/opt/labwatch#$CODE_DIR#g" -e "s#/var/lib/labwatch#$DATA_DIR#g" -e "s#^User=labwatch#User=$SERVICE_USER#" \
  -e "s#^Group=labwatch#Group=$SERVICE_USER#" "$CODE_DIR/deploy/labwatch.service" > "$UNIT_DIR/labwatch.service"
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable labwatch.service >/dev/null

if [[ "${NEW_CONFIG:-0}" == 1 ]]; then
  say "installed but NOT started. Edit $DATA_DIR/config.yaml and .env, then:"
  echo "    labwatch check && sudo systemctl start labwatch"
  exit 0
fi
if [[ "${NO_START:-0}" == 1 ]]; then
  say "installed/updated but not (re)started (NO_START=1): sudo systemctl restart labwatch"
  exit 0
fi

say "checking that the devices are reachable from this server"
if ! "$BIN_DIR/labwatch" check >/tmp/labwatch-check.$$ 2>&1; then
  grep -E '^== |ERROR|error' /tmp/labwatch-check.$$ || cat /tmp/labwatch-check.$$
  rm -f /tmp/labwatch-check.$$
  say "some device is not reachable from here - fix that (firewall/VPN?), then: sudo systemctl start labwatch"
  exit 1
fi
grep '^== ' /tmp/labwatch-check.$$ || true
rm -f /tmp/labwatch-check.$$

say "starting labwatch"
$SYSTEMCTL restart labwatch.service
sleep 3
$SYSTEMCTL --no-pager --lines=8 status labwatch.service || true
echo
echo "Logs:     journalctl -u labwatch -f"
echo "Status:   labwatch status        Mute:  labwatch mute Flash --for 2h"
echo "Remember: stop the old instance (only one per Telegram bot!)"
