#!/usr/bin/env bash
set -euo pipefail

# Configuration
PROJECT="${PROJECT:-s3monitoring}"
ENV="${ENV:-prod}"
AWS_REGION="${AWS_REGION:-eu-west-3}"
S3_BUCKET="${S3_BUCKET:-${PROJECT}-artifacts}"

# Build
echo "==> Building..."
sam build -b ./build

# Deploy
echo "==> Deploying ${PROJECT}-${ENV}..."
sam deploy \
  --template-file build/template.yaml \
  --stack-name "${PROJECT}-${ENV}" \
  --s3-bucket "${S3_BUCKET}" \
  --region "${AWS_REGION}" \
  --no-fail-on-empty-changeset \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ENV="${ENV}" \
    PROJECT="${PROJECT}" \
    AWSREGION="${AWS_REGION}" \
    S3PREFIX="${S3PREFIX:-backup}" \
    BUCKETSBLACKLIST="${BUCKETSBLACKLIST:-}" \
    SLACKWEBHOOKURL="${SLACK_WEBHOOK_URL:-}" \
    SIZETHRESHOLDPERCENT="${SIZE_THRESHOLD_PERCENT:-50}"

echo "==> Done!"
