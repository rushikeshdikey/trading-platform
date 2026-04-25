#!/bin/bash
# Cloud-init runs as root on first boot. Idempotent — re-running won't break.
set -euxo pipefail

# 1. Install Docker + Compose plugin from Amazon Linux 2023 repos.
dnf install -y docker
systemctl enable --now docker

# Compose v2 plugin install: AL2023 doesn't ship it, fetch from GH releases.
COMPOSE_VERSION="v2.29.7"
ARCH="$(uname -m)"
mkdir -p /usr/libexec/docker/cli-plugins
curl -sSL -o /usr/libexec/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}"
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# 2. Add the default user to the docker group so deploy scripts don't need sudo.
usermod -aG docker ec2-user || true

# 3. Lay out the deploy directory. CI rsyncs Caddyfile + docker-compose.yml
# here on each deploy; .env is created manually once and never overwritten.
install -d -o ec2-user -g ec2-user /opt/trading
install -d -o ec2-user -g ec2-user /opt/trading/secrets

# 4. Systemd unit so `systemctl status trading` works and Compose comes back
# up after reboots.
cat >/etc/systemd/system/trading.service <<'UNIT'
[Unit]
Description=Trading Journal stack (docker compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/trading
EnvironmentFile=-/opt/trading/.env
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable trading.service
# We don't `start` here because /opt/trading/.env + docker-compose.yml haven't
# been uploaded yet on first boot. The CI deploy script starts it.

echo "user_data done at $(date -u)" >>/var/log/trading-bootstrap.log
