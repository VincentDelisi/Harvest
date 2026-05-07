#!/usr/bin/env bash
# Install Harvest on a fresh Ubuntu 22.04+ VPS.
#
# Run as root (or sudo) on the VPS:
#   curl -fsSL https://raw.githubusercontent.com/VincentDelisi/Harvest/main/deploy/install.sh | sudo bash
#
# Or, after cloning the repo:
#   sudo bash deploy/install.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/VincentDelisi/Harvest.git}"
INSTALL_DIR="/opt/harvest"
SERVICE_USER="harvest"

echo "==> Installing system dependencies..."
apt-get update -qq
# Use the system's default python3 (3.10 on Ubuntu 22.04, 3.12 on Ubuntu 24.04).
apt-get install -y -qq python3 python3-venv python3-pip git tzdata

# Detect python3 version for downstream commands.
PYTHON_BIN="$(command -v python3)"
PY_VER="$(${PYTHON_BIN} -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "==> Using ${PYTHON_BIN} (Python ${PY_VER})"

echo "==> Setting timezone to America/New_York..."
timedatectl set-timezone America/New_York || true

echo "==> Creating service user '${SERVICE_USER}'..."
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Cloning repo to ${INSTALL_DIR}..."
if [ ! -d "${INSTALL_DIR}" ]; then
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi
cd "${INSTALL_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Setting up Python virtualenv..."
sudo -u "${SERVICE_USER}" "${PYTHON_BIN}" -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet -r requirements.txt

echo "==> Setting up logging directory..."
mkdir -p /var/log/harvest
chown -R "${SERVICE_USER}":"${SERVICE_USER}" /var/log/harvest

echo "==> Setting up data directory..."
mkdir -p "${INSTALL_DIR}/data"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}/data"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  echo "==> Creating .env from .env.example — EDIT THIS BEFORE STARTING THE SERVICE"
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
fi

echo "==> Installing systemd service..."
cp "${INSTALL_DIR}/deploy/harvest.service" /etc/systemd/system/harvest.service
systemctl daemon-reload

cat <<EOF

==============================================================
Install complete!

Next steps:
  1. Edit credentials:
       sudo -u harvest nano ${INSTALL_DIR}/.env

  2. Start with mode=DRY_RUN first. In .env:
       ENGINE_MODE=DRY_RUN

  3. Verify Polygon + Public connectivity:
       sudo -u harvest ${INSTALL_DIR}/.venv/bin/python -m scripts.check_today

  4. Start the service:
       sudo systemctl enable --now harvest
       sudo systemctl status harvest

  5. Tail logs:
       tail -f /var/log/harvest/engine.log

  6. To switch to live trading after dry-run validation:
       sudo -u harvest sed -i 's/ENGINE_MODE=DRY_RUN/ENGINE_MODE=LIVE_SMALL/' ${INSTALL_DIR}/.env
       sudo systemctl restart harvest
==============================================================
EOF
