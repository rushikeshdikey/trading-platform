#!/usr/bin/env bash
# First-run bootstrap for the EC2 instance — creates /opt/trading/.env with
# strong random secrets and uploads docker-compose.yml + Caddyfile.
#
# Run from your laptop:
#
#   ./infra/scripts/bootstrap.sh \
#       --instance-id i-0123456789abcdef0 \
#       --domain trading.zilionix.com \
#       --acme-email you@example.com \
#       --image ghcr.io/rushikeshdikey/trading-platform:main
#
# Idempotent: if /opt/trading/.env already exists, the script EXITS rather
# than overwriting your secrets. Force re-bootstrap with --force.
set -euo pipefail

INSTANCE_ID=""
DOMAIN=""
ACME_EMAIL=""
IMAGE="ghcr.io/rushikeshdikey/trading-platform:main"
REGION="ap-south-1"
FORCE=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --acme-email) ACME_EMAIL="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -z "$INSTANCE_ID" ]] && { echo "--instance-id required"; exit 1; }
[[ -z "$DOMAIN"      ]] && { echo "--domain required";      exit 1; }
[[ -z "$ACME_EMAIL"  ]] && { echo "--acme-email required";  exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Generate secrets locally — never sent over the wire as plaintext args.
SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n=' | head -c 64)
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '\n=/+' | head -c 32)

ENV_CONTENT=$(cat <<EOF
APP_DOMAIN=${DOMAIN}
ACME_EMAIL=${ACME_EMAIL}
APP_IMAGE=${IMAGE}
SECRET_KEY=${SECRET_KEY}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
KITE_REDIRECT_URL=https://${DOMAIN}/auth/zerodha/callback
EOF
)

# Encode artefacts so we can pipe them through SSM without escaping pain.
ENV_B64=$(echo "$ENV_CONTENT" | base64)
COMPOSE_B64=$(base64 < "${REPO_ROOT}/docker-compose.yml")
CADDY_B64=$(base64 < "${REPO_ROOT}/Caddyfile")

GUARD=""
if [[ $FORCE -eq 0 ]]; then
  GUARD="if [ -f /opt/trading/.env ]; then echo 'EXISTS — skipping (use --force to overwrite)' >&2; exit 0; fi"
fi

echo "→ Sending bootstrap to ${INSTANCE_ID} in ${REGION}..."

CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "Bootstrap /opt/trading/.env" \
  --parameters commands="[
    \"set -e\",
    \"$GUARD\",
    \"install -d -o ec2-user -g ec2-user /opt/trading\",
    \"echo '$ENV_B64' | base64 -d > /opt/trading/.env\",
    \"chmod 600 /opt/trading/.env\",
    \"chown ec2-user:ec2-user /opt/trading/.env\",
    \"echo '$COMPOSE_B64' | base64 -d > /opt/trading/docker-compose.yml\",
    \"echo '$CADDY_B64' | base64 -d > /opt/trading/Caddyfile\",
    \"chown ec2-user:ec2-user /opt/trading/docker-compose.yml /opt/trading/Caddyfile\",
    \"systemctl enable --now trading\",
    \"sleep 2\",
    \"sudo -u ec2-user docker compose -f /opt/trading/docker-compose.yml ps\"
  ]" \
  --query "Command.CommandId" --output text)

echo "→ Command ID: $CMD_ID — waiting..."
aws ssm wait command-executed --region "$REGION" \
  --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" || true

aws ssm get-command-invocation --region "$REGION" \
  --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query "{ status: Status, stdout: StandardOutputContent, stderr: StandardErrorContent }" \
  --output table

echo
echo "✅ Bootstrap complete"
echo
echo "  Domain:   ${DOMAIN}"
echo "  Image:    ${IMAGE}"
echo "  Secrets generated and stored in /opt/trading/.env (mode 600)"
echo
echo "Next: add an A record for ${DOMAIN} pointing to your Elastic IP at"
echo "      Namecheap, then visit https://${DOMAIN} once DNS propagates."
