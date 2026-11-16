#!/usr/bin/env bash
# Set up RMS Race Day on a fresh Ubuntu server (Oracle Cloud Always Free, Google Cloud e2-micro, any VPS).
#
#   sudo git clone https://github.com/zmbelles/RMS-race-day-app.git /opt/rms-race-day
#   sudo bash /opt/rms-race-day/deploy/setup.sh rmsracing.duckdns.org
#
# Installs Caddy (HTTPS certificates, renewed automatically), runs the app with waitress as a service that starts on
# boot, opens ports 80/443 in the server's firewall, and backs up the database every night. Safe to run again, e.g. to
# switch to a new domain: it keeps the existing database, password and secret key.
set -euo pipefail

DOMAIN="${1:?Usage: sudo bash deploy/setup.sh your.domain}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/rms-race-day
CONF_DIR=/etc/rms-race-day
APP_USER=rms

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

echo "== Packages"
apt-get update -q
apt-get install -y -q python3-venv git curl gpg debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

echo "== App"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g "$APP_USER" -m 750 "$CONF_DIR"

if [[ ! -s "$CONF_DIR/admin_password_hash" ]]; then
  echo "Choose the team password (10+ characters):"
  "$APP_DIR/.venv/bin/python" - "$CONF_DIR/admin_password_hash" <<'PY'
import getpass, sys
from werkzeug.security import generate_password_hash
pw = getpass.getpass("Password: ")
if len(pw) < 10 or pw != getpass.getpass("Again: "):
    sys.exit("Passwords must match and be at least 10 characters.")
open(sys.argv[1], "w").write(generate_password_hash(pw))
PY
fi
if [[ ! -s "$CONF_DIR/env" ]]; then
  cat > "$CONF_DIR/env" <<EOF
RMS_ENV=production
SECRET_KEY=$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
ADMIN_PASSWORD_HASH_FILE=$CONF_DIR/admin_password_hash
RMS_DB_PATH=$DATA_DIR/rms_race_day.db
RMS_TZ=America/Chicago
TRUST_PROXY=1
EOF
fi
chown root:"$APP_USER" "$CONF_DIR/env" "$CONF_DIR/admin_password_hash"
chmod 640 "$CONF_DIR/env" "$CONF_DIR/admin_password_hash"

sed "s#__APP_DIR__#$APP_DIR#g; s#__DATA_DIR__#$DATA_DIR#g; s#__CONF_DIR__#$CONF_DIR#g; s#__APP_USER__#$APP_USER#g" \
  "$APP_DIR/deploy/rms-race-day.service" > /etc/systemd/system/rms-race-day.service
systemctl daemon-reload
systemctl enable --now rms-race-day
systemctl restart rms-race-day

echo "== HTTPS"
sed "s#__DOMAIN__#$DOMAIN#g" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "== Firewall"
# Oracle's Ubuntu images block everything but SSH in iptables, on top of the cloud's own security list.
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null \
    || iptables -I INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
done
command -v netfilter-persistent >/dev/null && netfilter-persistent save

echo "== Nightly backup"
cat > /etc/cron.d/rms-race-day-backup <<EOF
# Copy the database at 3am every night and keep two weeks of copies.
0 3 * * * $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/deploy/backup.py $DATA_DIR/rms_race_day.db $DATA_DIR/backups 14
EOF

echo
echo "Done. Open https://$DOMAIN (the first visit can take a minute while the certificate is issued)."
echo "Status: systemctl status rms-race-day   Logs: journalctl -u rms-race-day -f"
