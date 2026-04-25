# Trading Journal — AWS infrastructure

Single-VM deploy on AWS, optimized for **<$10/month** total cost. Architecture:

- **EC2 t4g.micro** (ARM, free-tier 12 months → ~$6/mo after) running Docker Compose.
- **EBS gp3 20 GB** root volume (~$1.60/mo) for OS + DB + image cache.
- **Elastic IP** (free while attached) so DNS at Namecheap never has to change.
- **SSM Session Manager** for ops access — no public SSH required.
- **Caddy** in the compose stack auto-provisions Let's Encrypt TLS.

## First-time setup

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — set admin_ip_cidr to your IP
terraform init
terraform plan
terraform apply
```

After apply, copy the `eip_address` output and:

1. **DNS**: At Namecheap, add an A record `trading` → `<eip_address>` on `zilionix.com`.
2. **Open a shell on the box** (no SSH needed):
   ```bash
   aws ssm start-session --target $(terraform output -raw instance_id) --region ap-south-1
   ```
3. **Bootstrap secrets** on the instance (`/opt/trading/.env`):
   ```ini
   APP_DOMAIN=trading.zilionix.com
   ACME_EMAIL=you@example.com
   APP_IMAGE=ghcr.io/rushikeshdikey/trading-platform:main
   POSTGRES_PASSWORD=<strong random>
   SECRET_KEY=<32+ chars, generate with `openssl rand -base64 48`>
   KITE_API_KEY=...
   KITE_API_SECRET=...
   KITE_REDIRECT_URL=https://trading.zilionix.com/auth/zerodha/callback
   ```
4. **Upload `docker-compose.yml` and `Caddyfile`** to `/opt/trading/`.
   The deploy script in `infra/scripts/deploy.sh` automates this from CI.
5. **Start**: `sudo systemctl start trading`. Watch with `sudo docker compose logs -f`.

## Cost ceiling

| Resource         | Free-tier? | Steady-state          |
|------------------|------------|-----------------------|
| EC2 t4g.micro    | Year 1     | ~$6.00/mo             |
| EBS gp3 20 GB    | No         | ~$1.60/mo             |
| Elastic IP       | Free attached | Free if always in use |
| Data egress      | 100 GB free / mo | ~$0 at this scale  |
| SSM              | Free       | Free                  |
| **Total Y1**     |            | **< $2/mo**           |
| **Total Y2+**    |            | **~$8/mo**            |

If usage outgrows t4g.micro (Postgres + app + Caddy on 1 GB RAM gets tight),
flip `instance_type = "t4g.small"` (~$13/mo) — no other change needed.

## State

State is stored locally for v1 (`terraform.tfstate`). When more than one
operator manages this, migrate to an S3 backend:

```hcl
terraform {
  backend "s3" {
    bucket         = "zilionix-terraform-state"
    key            = "trading-platform/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "zilionix-tf-lock"
    encrypt        = true
  }
}
```

## Multi-cloud option

The Dockerfile + docker-compose.yml is portable. To move to GCP:
- Provision a Compute Engine `e2-micro` (free tier in us-* regions).
- Same compose stack, same image.
- Cloud DNS or Namecheap update with the new IP.

This Terraform doesn't try to be a multi-cloud abstraction — that costs more
than it saves at this scale. If credits expire, redeploy once on the cheapest
new home.
