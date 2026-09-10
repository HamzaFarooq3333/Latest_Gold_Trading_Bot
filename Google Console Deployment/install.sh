#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/asim-gcp"
SERVICE="asim-gcp.service"
DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "[asim-gcp] installing to ${APP_DIR} from ${DEPLOY_ROOT}"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip rsync curl nginx openssl
sudo mkdir -p "${APP_DIR}/data" "${APP_DIR}/bridge-bundle"
sudo rsync -a --delete "${DEPLOY_ROOT}/app/" "${APP_DIR}/"
sudo mkdir -p "${APP_DIR}/certs"
if [ -f "${DEPLOY_ROOT}/.env" ]; then
  cp -f "${DEPLOY_ROOT}/.env" "${APP_DIR}/.env"
elif [ -f "${DEPLOY_ROOT}/.env.example" ]; then
  cp -f "${DEPLOY_ROOT}/.env.example" "${APP_DIR}/.env"
fi
if ! grep -q '^AUTH_SECRET=' "${APP_DIR}/.env" 2>/dev/null; then
  echo "AUTH_SECRET=$(openssl rand -hex 32)" >> "${APP_DIR}/.env"
fi
grep -q '^BEHIND_HTTPS=' "${APP_DIR}/.env" || echo "BEHIND_HTTPS=1" >> "${APP_DIR}/.env"
sudo chown -R "${USER}:${USER}" "${APP_DIR}"

if [ ! -f "${APP_DIR}/certs/cert.pem" ]; then
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "${APP_DIR}/certs/key.pem" \
    -out "${APP_DIR}/certs/cert.pem" \
    -subj "/CN=asim-gcp/O=Onyxion"
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
