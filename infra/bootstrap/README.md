# infra/bootstrap

Applied **once**, stays up (costs cents). Everything that must survive the nightly `terraform destroy` of `envs/dev`:

| Resource | Why here |
|---|---|
| S3 state bucket `eventpark-tfstate-<account>` (versioned, SSE-S3, private, HTTPS-only) | remote state + native S3 locking for every stack |
| ECR `eventpark/backend`, `eventpark/fakepay` (immutable tags, scan on push, keep 15) | images survive destroy |
| GitHub OIDC provider + role `eventpark-github-deploy` | CI deploys without stored AWS keys; only `main` of this repo |
| SNS topic `eventpark-alarms` + email subscription | CloudWatch alarms (Phase 14) |

Budgets were created in the console in Phase 0 (the one allowed click-ops) and are not managed here.

## First apply (needs Damjan: `aws login` + explicit go-ahead)

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars        # your alarm email
terraform init                                      # local state for now
terraform plan -out bootstrap.plan                  # READ IT: ~15 resources, nothing that costs per hour
terraform apply bootstrap.plan
# confirm the SNS email subscription (link in your inbox)
```

## Move the state into the bucket it created (chicken and egg)

1. Uncomment the `backend "s3"` block in `versions.tf`, fill in `terraform output -raw state_bucket`.
2. `terraform init -migrate-state` → answer `yes`. The local `terraform.tfstate` can then be deleted.

## Push images by hand (once, to learn what CI will do)

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.eu-central-1.amazonaws.com
aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY   # 12-hour token
SHA=$(git rev-parse --short HEAD)
docker build -t $REGISTRY/eventpark/backend:$SHA backend && docker push $REGISTRY/eventpark/backend:$SHA
docker build -t $REGISTRY/eventpark/fakepay:$SHA fakepay && docker push $REGISTRY/eventpark/fakepay:$SHA
```

## Cost

S3 state (KB) + ECR (~0.5 GB × $0.10) + SNS email (free tier) ≈ **$0.05/month**. No hourly cost.
