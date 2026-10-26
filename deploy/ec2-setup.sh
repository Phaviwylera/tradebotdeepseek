#!/usr/bin/env bash
# One-shot EC2 bootstrap for the Polymarket bot.
#
#   curl -fsSL https://raw.githubusercontent.com/Phaviwylera/tradebotdeepseek/main/deploy/ec2-setup.sh | sudo bash
#
# or, from a checkout:
#
#   sudo bash deploy/ec2-setup.sh
#
# What it does, idempotently:
#   1. installs docker + compose plugin (Ubuntu/Debian or Amazon Linux);
#   2. clones or updates the repository at /opt/polymarket-bot;
#   3. creates .env from .env.aws.example with a generated DASHBOARD_TOKEN,
#      mode 600, never overwriting an existing file;
#   4. builds and starts the container, then waits for /healthz;
#   5. installs a systemd unit so the stack returns after a reboot.
#
# It deliberately leaves LIVE=false. Going live is a manual, audited step.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Phaviwylera/tradebotdeepseek.git}"
APP_DIR="${APP_DIR:-/opt/polymarket-bot}"
BRANCH="${BRANCH:-main}"
PORT="${API_PORT:-8099}"

log() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m warn\033[0m %s\n' "$*"; }

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (sudo bash $0)." >&2
  exit 1
fi

# ---------------------------------------------------------------- 1. docker
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y ca-certificates curl git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      -o /etc/apt/keyrings/docker.asc 2>/dev/null || \
      curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y docker git
    systemctl enable --now docker
    mkdir -p /usr/libexec/docker/cli-plugins
    curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
      -o /usr/libexec/docker/cli-plugins/docker-compose
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose
  else
    echo "Unsupported package manager; install Docker manually." >&2
    exit 1
  fi
else
  log "Docker already present"
fi

systemctl enable --now docker

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is missing" >&2
  exit 1
fi

# ------------------------------------------------------------------ 2. code
if [[ -d "${APP_DIR}/.git" ]]; then
  log "Updating ${APP_DIR} (${BRANCH})"
  git -C "${APP_DIR}" fetch --quiet origin "${BRANCH}"
  git -C "${APP_DIR}" checkout --quiet "${BRANCH}"
  git -C "${APP_DIR}" pull --ff-only --quiet origin "${BRANCH}"
else
  log "Cloning into ${APP_DIR}"
  install -d -m 0755 "${APP_DIR}"
  git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi

# ------------------------------------------------------------- 3. env file
if [[ ! -f "${APP_DIR}/.env" ]]; then
  log "Creating .env with a fresh dashboard token"
  token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cp "${APP_DIR}/.env.aws.example" "${APP_DIR}/.env"
  sed -i "s/^DASHBOARD_TOKEN=.*/DASHBOARD_TOKEN=${token}/" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  echo "   DASHBOARD_TOKEN saved to ${APP_DIR}/.env (mode 600)"
else
  warn "${APP_DIR}/.env already exists; leaving it untouched"
fi

if grep -qE '^LIVE=true' "${APP_DIR}/.env"; then
  warn "LIVE=true is set in ${APP_DIR}/.env — real orders will be attempted"
fi

# --------------------------------------------------------------- 4. start
log "Building and starting the stack"
cd "${APP_DIR}"
docker compose up -d --build --remove-orphans

log "Waiting for the read-only status server on 127.0.0.1:${PORT}"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "${healthy:-0}" == "1" ]]; then
  log "Healthy: http://127.0.0.1:${PORT}/healthz"
else
  warn "No healthy /healthz yet. Inspect: docker compose logs --tail=80 bot"
fi

docker compose ps
docker compose logs --tail=25 bot || true

# --------------------------------------------------------------- 5. systemd
install -m 0644 "${APP_DIR}/deploy/polymarket-bot.service" /etc/systemd/system/polymarket-bot.service
systemctl daemon-reload
systemctl enable polymarket-bot.service >/dev/null 2>&1 || true

cat <<EOF

Done.

  status     docker compose -f ${APP_DIR}/docker-compose.yml ps
  logs       docker compose -f ${APP_DIR}/docker-compose.yml logs -f bot
  dashboard  ssh -N -L ${PORT}:127.0.0.1:${PORT} \$(whoami)@<this-host>
             then open http://127.0.0.1:${PORT}
  token      grep DASHBOARD_TOKEN ${APP_DIR}/.env

The bot is running in DRY-RUN (LIVE=false in .env). Read-only means exactly
that: the console can show state, never place or cancel an order.
EOF
