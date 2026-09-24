#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/asim-gcp"
SERVICE="asim-gcp.service"
DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "[asim-gcp] installing to ${APP_DIR} from ${DEPLOY_ROOT}"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip rsync curl nginx openssl
sudo mkdir -p "${APP_DIR}/data" "${APP_DIR}/bridge-bundle" "${APP_DIR}/certs"

# Never wipe live desk data, TLS certs, venv, or .env on redeploy.
# A plain `rsync --delete app/ -> APP_DIR/` deleted lab_store + certs (2026-09-24 outage).
sudo rsync -a --delete \
  --exclude 'data/' \
  --exclude 'certs/' \
  --exclude 'venv/' \
  --exclude '.env' \
  --exclude 'bridge-bundle/' \
  "${DEPLOY_ROOT}/app/" "${APP_DIR}/"

if [ -f "${DEPLOY_ROOT}/.env" ]; then
  # Preserve AUTH_SECRET if the live .env already has one (keeps sessions valid).
  if [ -f "${APP_DIR}/.env" ] && grep -q '^AUTH_SECRET=' "${APP_DIR}/.env"; then
    OLD_SECRET="$(grep '^AUTH_SECRET=' "${APP_DIR}/.env" | head -1)"
    cp -f "${DEPLOY_ROOT}/.env" "${APP_DIR}/.env"
    if ! grep -q '^AUTH_SECRET=' "${APP_DIR}/.env"; then
      echo "${OLD_SECRET}" >> "${APP_DIR}/.env"
    else
      # keep the running secret
      tmp="$(mktemp)"
      grep -v '^AUTH_SECRET=' "${APP_DIR}/.env" > "${tmp}"
      echo "${OLD_SECRET}" >> "${tmp}"
      mv "${tmp}" "${APP_DIR}/.env"
    fi
  else
    cp -f "${DEPLOY_ROOT}/.env" "${APP_DIR}/.env"
  fi
elif [ -f "${DEPLOY_ROOT}/.env.example" ] && [ ! -f "${APP_DIR}/.env" ]; then
  cp -f "${DEPLOY_ROOT}/.env.example" "${APP_DIR}/.env"
fi
if [ -f "${APP_DIR}/.env" ] && ! grep -q '^AUTH_SECRET=' "${APP_DIR}/.env" 2>/dev/null; then
  echo "AUTH_SECRET=$(openssl rand -hex 32)" >> "${APP_DIR}/.env"
fi
grep -q '^BEHIND_HTTPS=' "${APP_DIR}/.env" 2>/dev/null || echo "BEHIND_HTTPS=1" >> "${APP_DIR}/.env"
sudo chown -R "${USER}:${USER}" "${APP_DIR}"

if [ ! -f "${APP_DIR}/certs/cert.pem" ] || [ ! -f "${APP_DIR}/certs/key.pem" ]; then
  echo "[asim-gcp] generating new TLS cert (browser will warn once)"
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "${APP_DIR}/certs/key.pem" \
    -out "${APP_DIR}/certs/cert.pem" \
    -subj "/CN=asim-gcp/O=Onyxion"
else
  echo "[asim-gcp] keeping existing TLS certs"
fi

if [ ! -d "${APP_DIR}/venv" ]; then
  python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [ -f "${DEPLOY_ROOT}/asim-gcp.service" ]; then
  sed "s/__DEPLOY_USER__/${USER}/g" "${DEPLOY_ROOT}/asim-gcp.service" | sudo tee "/etc/systemd/system/${SERVICE}" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE}"
  sudo systemctl restart "${SERVICE}"
fi

if [ -f "${DEPLOY_ROOT}/nginx-asim-gcp.conf" ]; then
  sudo cp "${DEPLOY_ROOT}/nginx-asim-gcp.conf" /etc/nginx/sites-available/asim-gcp
  sudo ln -sf /etc/nginx/sites-available/asim-gcp /etc/nginx/sites-enabled/asim-gcp
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
  sudo systemctl enable nginx
  sudo systemctl restart nginx
fi

sleep 2
sudo systemctl is-active "${SERVICE}" || (sudo journalctl -u "${SERVICE}" -n 40 --no-pager; exit 1)
sudo systemctl is-active nginx || (sudo journalctl -u nginx -n 40 --no-pager; exit 1)
curl -fsSk "https://127.0.0.1/health" | head -c 400
echo
curl -fsSk "https://127.0.0.1/api/info" -u "$(grep '^AUTH_USER=' "${APP_DIR}/.env" | cut -d= -f2-):$(grep '^AUTH_PASS=' "${APP_DIR}/.env" | cut -d= -f2-)" 2>/dev/null | head -c 200 || true
echo
echo "[asim-gcp] install complete (HTTPS :443, app on 127.0.0.1:8080)"
