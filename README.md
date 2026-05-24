# S3 Backup Notifier

Serverless AWS Lambda that monitors your S3 backup buckets daily and alerts you via Slack when something goes wrong.

## What it does

| Check | Alert |
|---|---|
| No backup file received today | `S3 Backup failed` |
| Today's backup is abnormally small compared to the previous one | `S3 Backup suspicious size` |

The size comparison catches silent failures: a backup job that runs but produces a near-empty dump, a partial export, or a corrupted archive.

## How it works

```
CloudWatch Events (daily cron)
        |
        v
   AWS Lambda (Python)
        |
        +--> List all S3 buckets matching a prefix
        +--> Skip blacklisted buckets
        +--> For each bucket:
        |       - Check if a file was uploaded today
        |       - Compare today's file size vs previous day
        |
        +--> Slack webhook notification on failure
```

## Configuration

| Environment Variable | Description | Default |
|---|---|---|
| `S3PREFIX` | Bucket name prefix to monitor | - |
| `BUCKETSBLACKLIST` | Comma-separated bucket names to skip | - |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL | - |
| `SIZE_THRESHOLD_PERCENT` | Alert if today's size < X% of previous | `50` |
| `AWSREGION` | AWS region | `eu-west-3` |
| `BUCKET_COMPONENTS` | JSON mapping bucket → expected components (`db`, `etc`, `boot`, `site`) | `{}` |
| `B2_BUCKETS` | Comma-separated B2 bucket names to monitor (in addition to AWS) | empty |
| `B2_ENDPOINT_URL` | B2 S3-compatible endpoint URL (e.g. `https://s3.eu-central-003.backblazeb2.com`) | empty |
| `B2_SECRET_ARN` | ARN of a Secrets Manager secret containing `{"key_id","application_key"}` | empty |

### Multi-cloud monitoring (B2)

The Lambda can monitor both AWS S3 and Backblaze B2 buckets in a single pass. AWS buckets are discovered by enumeration; B2 buckets must be listed explicitly in `B2_BUCKETS` because there is no cross-account `list-buckets` for B2.

To enable:

1. Create a Secrets Manager secret in the same region as the Lambda:
   ```bash
   aws secretsmanager create-secret \
     --name s3monitoring/b2-credentials \
     --secret-string '{"key_id":"...","application_key":"..."}' \
     --region eu-west-3
   ```
2. Set `B2_BUCKETS`, `B2_ENDPOINT_URL`, `B2_SECRET_ARN` (env vars or stack parameters).
3. The Lambda IAM role gets `secretsmanager:GetSecretValue` only when `B2_SECRET_ARN` is non-empty (CloudFormation `Conditions`).

When B2 is configured, the daily report appends a `[b2]` tag to B2-hosted bucket lines so you can tell them apart.

## Deployment

### Prerequisites

- AWS CLI configured
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed
- An S3 bucket named `<project>-artifacts` for deployment artifacts

### Quick deploy

```bash
export S3PREFIX=backup
export BUCKETSBLACKLIST=bucket-to-skip-1,bucket-to-skip-2
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/xxx/xxx
export SIZE_THRESHOLD_PERCENT=50

./deploy.sh
```

### CI/CD

Deployment is automated via [GitHub Actions](.github/workflows/main.yml) on push to `master`.

Required GitHub secrets:

| Secret | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials |
| `ROLE_TO_ASSUME` | IAM role ARN for deployment |
| `REGION` | AWS region |
| `PROJECT` | Project name (default: `s3monitoring`) |
| `ENV` | Environment (e.g. `prod`) |
| `S3PREFIX` | Bucket prefix to monitor |
| `BUCKETSBLACKLIST` | Buckets to exclude |
| `SLACK_WEBHOOK_URL` | Slack webhook URL |
| `SIZE_THRESHOLD_PERCENT` | Size alert threshold |

### Manual deploy (Makefile)

```bash
# Build the Lambda layer
make layer

# Package
make package PROJECT=s3monitoring

# Deploy
make deploy PROJECT=s3monitoring ENV=prod

# Cleanup build artifacts
make cleaning

# Destroy the stack
make tear-down
```

## Slack notifications

**Missing backup:**
> **S3 Backup failed** backup-mydb
> Last backup comes from:
> Date: 2026-04-05
> Name: mydb-dump.sql.gz
> Size: 12.4GiB

**Suspicious size:**
> **S3 Backup suspicious size** backup-mydb
> Today's backup is abnormally small:
> Today: mydb-dump.sql.gz (1.2MiB)
> Previous: mydb-dump.sql.gz (12.4GiB)
