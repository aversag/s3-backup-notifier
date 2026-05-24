import datetime
import json
import os
import re

import boto3
import botocore
import requests

# Config. Read with safe defaults so this module can be imported by other
# handlers in the same package (e.g. alarm_forwarder) that don't need the
# full s3-monitor configuration set.
bucket_blacklist = os.environ.get('BUCKETSBLACKLIST', '').split(",")
s3_prefix = os.environ.get('S3PREFIX', 'backup')
slack_webhook_url = os.environ.get('SLACK_WEBHOOK_URL')
aws_region = os.environ.get('AWSREGION', 'eu-west-3')

# Optional per-bucket expected components, e.g.:
# {"my-full-backup-bucket": ["etc","boot","site","db"], "my-db-only-bucket": ["db"]}
try:
    bucket_components = json.loads(os.environ.get('BUCKET_COMPONENTS', '{}'))
except json.JSONDecodeError:
    bucket_components = {}

# Backblaze B2 config (optional — empty means AWS-only monitoring).
b2_buckets = [b.strip() for b in os.environ.get('B2_BUCKETS', '').split(',') if b.strip()]
b2_endpoint_url = os.environ.get('B2_ENDPOINT_URL', '')
b2_secret_arn = os.environ.get('B2_SECRET_ARN', '')

# Get Today's date
today = datetime.date.today()

# AWS Connection
aws_session = boto3.Session(region_name=aws_region)
aws_s3 = aws_session.resource('s3')

# Lazy B2 client — built on first call so we don't pay the Secrets Manager
# lookup unless B2 monitoring is actually configured.
_b2_s3 = None


def _get_b2_s3():
    global _b2_s3
    if _b2_s3 is not None:
        return _b2_s3
    if not (b2_buckets and b2_endpoint_url and b2_secret_arn):
        return None
    sm = aws_session.client('secretsmanager')
    raw = sm.get_secret_value(SecretId=b2_secret_arn)['SecretString']
    creds = json.loads(raw)
    b2_session = boto3.Session(
        aws_access_key_id=creds['key_id'],
        aws_secret_access_key=creds['application_key'],
    )
    _b2_s3 = b2_session.resource('s3', endpoint_url=b2_endpoint_url)
    return _b2_s3


def _iter_target_buckets():
    """Yield (s3_resource, bucket_name, provider) for every bucket we should check.

    AWS buckets are discovered by enumerating the account; B2 buckets come from
    the explicit B2_BUCKETS env var because we can't list cross-account.
    """
    for bucket in aws_s3.buckets.all():
        if not bucket.name.startswith(s3_prefix):
            continue
        if bucket.name in bucket_blacklist:
            continue
        yield aws_s3, bucket.name, 'aws'

    if b2_buckets:
        b2 = _get_b2_s3()
        if b2 is None:
            print("WARN: B2_BUCKETS set but B2_ENDPOINT_URL/B2_SECRET_ARN missing — skipping B2")
            return
        for name in b2_buckets:
            if name in bucket_blacklist:
                continue
            yield b2, name, 'b2'


# Classification patterns. First match wins; 'site' is a fallback for archives that
# match none of the above.
COMPONENT_PATTERNS = [
    ('etc',  re.compile(r'(^|[-./_])etc[-.]', re.I)),
    ('boot', re.compile(r'(^|[-./_])boot[-.]', re.I)),
    ('db',   re.compile(r'(\.sql\.(gz|bz2|xz)$|\.dump$|\.sql$|_gitlab_backup\.tar$|-mysql-|-postgres-|\.pgdump$)', re.I)),
]
SITE_RX = re.compile(r'\.(tar(\.gz|\.bz2|\.xz)?|tgz|zip)$', re.I)


def classify(key):
    base = key.rsplit('/', 1)[-1]
    for cat, rx in COMPONENT_PATTERNS:
        if rx.search(base):
            return cat
    if SITE_RX.search(base):
        return 'site'
    return None


def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def _today_components(root_objs):
    """Return set of component categories present in today's root files."""
    found = set()
    for obj in root_objs:
        if obj.last_modified.date() != today:
            continue
        c = classify(obj.key)
        if c:
            found.add(c)
    return found


def _list_root_objs(s3_resource, bucket_name):
    """Return root-level objects only, sorted newest first.

    Using Delimiter='/' lets S3 skip everything under sub-prefixes server-side
    instead of streaming millions of keys to the Lambda just to filter them out.
    """
    bucket = s3_resource.Bucket(bucket_name)
    root_objs = list(bucket.objects.filter(Delimiter='/'))
    root_objs.sort(key=lambda o: o.last_modified, reverse=True)
    return root_objs


def main(event, context):
    size_threshold_percent = int(os.environ.get('SIZE_THRESHOLD_PERCENT', '50'))
    for s3_resource, name, provider in _iter_target_buckets():
        try:
            print(f"Bucket --> {name} ({provider})")
            root_objs = _list_root_objs(s3_resource, name)
            if not root_objs:
                continue

            today_objs = [obj for obj in root_objs if obj.last_modified.date() == today]

            # Group files by day and sum sizes for the last 3 days
            daily_sizes = {}
            for obj in root_objs:
                d = obj.last_modified.date()
                if d < today:
                    daily_sizes[d] = daily_sizes.get(d, 0) + obj.size

            for obj in root_objs:
                print(obj.last_modified.date(), obj.key, sizeof_fmt(obj.size))

            if not today_objs:
                last = root_objs[0]
                notification(
                    name,
                    file_date=last.last_modified.date(),
                    file_name=last.key,
                    file_size=sizeof_fmt(last.size),
                    alert_type="missing"
                )
                print("No backup detected from today: " + str(today))
                print("--> Last backup file: " + str(last.last_modified.date()), last.key, sizeof_fmt(last.size))
                continue

            today_total = sum(obj.size for obj in today_objs)
            print("Backup OK, All Good")
            print(f"--> Today: {len(today_objs)} files, total {sizeof_fmt(today_total)}")

            # Component-level check (only if bucket is configured)
            expected = bucket_components.get(name)
            if expected:
                found = _today_components(today_objs)
                missing = [c for c in expected if c not in found]
                if missing:
                    notification(
                        name,
                        file_date=today,
                        file_name=", ".join(missing),
                        file_size="-",
                        alert_type="components",
                        expected_components=expected,
                        found_components=sorted(found),
                    )
                    print(f"Missing components today: {missing} (expected {expected}, found {sorted(found)})")

            # Compare against average of last 3 days
            recent_days = sorted(daily_sizes.keys(), reverse=True)[:3]
            if recent_days:
                avg_size = sum(daily_sizes[d] for d in recent_days) / len(recent_days)
                if avg_size > 0 and today_total < avg_size * size_threshold_percent / 100:
                    notification(
                        name,
                        file_date=today,
                        file_name=f"{len(today_objs)} files",
                        file_size=sizeof_fmt(today_total),
                        alert_type="size",
                        prev_file_name=f"avg of last {len(recent_days)} days",
                        prev_file_size=sizeof_fmt(avg_size)
                    )
                    print(f"Size alert: today {sizeof_fmt(today_total)} vs avg {sizeof_fmt(avg_size)}")

        except botocore.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            print(e.response['Error']['Message'])
            if error_code == '404':
                print("There is no file in this bucket")
            else:
                print(e)


def notification(bucket_name, file_date, file_name, file_size, alert_type="missing",
                 prev_file_name=None, prev_file_size=None,
                 expected_components=None, found_components=None):
    if alert_type == "size":
        subject = f"S3 Backup suspicious size ⚠️ {bucket_name}"
        message = (
            f"S3 Backup Notifier\n"
            f"Today's backup total size is abnormally small:\n"
            f"Today: {file_name} — {file_size}\n"
            f"Previous: {prev_file_name} — {prev_file_size}"
        )
    elif alert_type == "components":
        subject = f"S3 Backup missing components ⚠️ {bucket_name}"
        message = (
            f"S3 Backup Notifier\n"
            f"Today's backup is missing expected components: {file_name}\n"
            f"Expected: {expected_components}\n"
            f"Found: {found_components}"
        )
    else:
        subject = f"S3 Backup failed ❌ {bucket_name}"
        message = (
            f"S3 Backup Notifier\n"
            f"Last backup comes from:\n"
            f"Date: {file_date}\n"
            f"Name: {file_name}\n"
            f"Size: {file_size}"
        )

    if slack_webhook_url:
        slack_payload = {
            "text": f"*{subject}*\n{message}"
        }
        try:
            response = requests.post(slack_webhook_url, json=slack_payload)
            response.raise_for_status()
            print("Slack notification sent!")
        except Exception as e:
            print(f"Slack notification failed: {e}")
    else:
        print("Slack webhook URL not configured. Notification not sent.")


def report(event, context):
    size_threshold_percent = int(os.environ.get('SIZE_THRESHOLD_PERCENT', '50'))
    lines = []
    ok_count = 0
    total_count = 0

    for s3_resource, name, provider in _iter_target_buckets():
        total_count += 1
        provider_tag = "" if provider == 'aws' else f" [{provider}]"

        try:
            root_objs = _list_root_objs(s3_resource, name)

            if not root_objs:
                lines.append(f"❌ `{name}`{provider_tag} — empty bucket")
                continue

            today_objs = [obj for obj in root_objs if obj.last_modified.date() == today]

            if not today_objs:
                last = root_objs[0]
                lines.append(f"❌ `{name}`{provider_tag} — no backup today (last: {last.last_modified.date()})")
                continue

            today_total = sum(obj.size for obj in today_objs)
            file_count = len(today_objs)

            # Compute variation against avg of last 3 days
            daily_sizes = {}
            for obj in root_objs:
                d = obj.last_modified.date()
                if d < today:
                    daily_sizes[d] = daily_sizes.get(d, 0) + obj.size

            recent_days = sorted(daily_sizes.keys(), reverse=True)[:3]
            variation = ""
            is_suspicious = False
            if recent_days:
                avg_size = sum(daily_sizes[d] for d in recent_days) / len(recent_days)
                if avg_size > 0:
                    pct = ((today_total - avg_size) / avg_size) * 100
                    arrow = "↑" if pct >= 0 else "↓"
                    variation = f" ({arrow} {pct:+.0f}%)"
                    if today_total < avg_size * size_threshold_percent / 100:
                        is_suspicious = True

            # Component check
            comp_warning = ""
            expected = bucket_components.get(name)
            if expected:
                found = _today_components(today_objs)
                missing = [c for c in expected if c not in found]
                if missing:
                    comp_warning = f" — missing: {','.join(missing)}"

            if is_suspicious or comp_warning:
                lines.append(f"⚠️ `{name}`{provider_tag} — {file_count} files, {sizeof_fmt(today_total)}{variation}{comp_warning}")
            else:
                lines.append(f"✅ `{name}`{provider_tag} — {file_count} files, {sizeof_fmt(today_total)}{variation}")
                ok_count += 1

        except botocore.exceptions.ClientError as e:
            lines.append(f"❌ `{name}`{provider_tag} — error: {e.response['Error']['Message']}")

    header = f"*📊 S3 Backup Report — {today}*\n"
    footer = f"\n*Total: {ok_count}/{total_count} buckets OK*"
    message = header + "\n".join(lines) + footer

    print(message)

    if slack_webhook_url:
        try:
            response = requests.post(slack_webhook_url, json={"text": message})
            response.raise_for_status()
            print("Report sent to Slack!")
        except Exception as e:
            print(f"Slack report failed: {e}")


def alarm_forwarder(event, context):
    """Forward CloudWatch alarm notifications (received via SNS) to Slack.

    Triggered by the alarmTopic SNS topic; one Lambda invocation per alarm
    state transition. Keeps the message tight so it's readable on mobile.
    """
    if not slack_webhook_url:
        print("No Slack webhook configured, skipping forward")
        return

    for record in event.get('Records', []):
        sns = record.get('Sns', {})
        raw = sns.get('Message', '')
        try:
            payload = json.loads(raw)
            alarm = payload.get('AlarmName', 'unknown alarm')
            state = payload.get('NewStateValue', 'UNKNOWN')
            reason = payload.get('NewStateReason', '')
            desc = payload.get('AlarmDescription', '')
            icon = "🚨" if state == "ALARM" else ("✅" if state == "OK" else "⚠️")
            text = f"*{icon} {alarm}* → `{state}`\n{desc}\n_{reason}_"
        except Exception:
            text = f"*AWS Alert*\n{raw[:1500]}"

        try:
            requests.post(slack_webhook_url, json={"text": text}, timeout=10).raise_for_status()
            print(f"Forwarded alarm: {raw[:120]}")
        except Exception as e:
            print(f"Slack forward failed: {e}")


# Run locally for testing purpose
if __name__ == '__main__':
    main(0, 0)
