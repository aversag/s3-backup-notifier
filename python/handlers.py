import datetime
import boto3
import botocore
import os
import requests

# Config
bucket_blacklist = os.environ['BUCKETSBLACKLIST'].split(",")
s3_prefix = os.environ['S3PREFIX']
slack_webhook_url = os.environ.get('SLACK_WEBHOOK_URL')
aws_region = os.environ['AWSREGION']
    
# Get Today's date
today = datetime.date.today()

# AWS Connection
session = boto3.Session(region_name=aws_region)
s3 = session.resource('s3')
bucket_names = s3.buckets.all()


# Convert to Human Readable
def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def main(event, context):
    size_threshold_percent = int(os.environ.get('SIZE_THRESHOLD_PERCENT', '50'))
    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue

        if bucket_name.name in bucket_blacklist:
            continue

        try:
            print("Bucket --> " + str(bucket_name.name))
            bucket = s3.Bucket(bucket_name.name)
            objs = bucket.objects.all()

            root_objs = sorted(
                [obj for obj in objs if '/' not in obj.key],
                key=lambda o: o.last_modified,
                reverse=True
            )
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
                    bucket_name.name,
                    file_date=last.last_modified.date(),
                    file_name=last.key,
                    file_size=sizeof_fmt(last.size),
                    alert_type="missing"
                )
                print("No backup detected from today: " + str(today))
                print("--> Last backup file: " + str(last.last_modified.date()), last.key, sizeof_fmt(last.size))
            else:
                today_total = sum(obj.size for obj in today_objs)
                print("Backup OK, All Good")
                print(f"--> Today: {len(today_objs)} files, total {sizeof_fmt(today_total)}")

                # Compare against average of last 3 days
                recent_days = sorted(daily_sizes.keys(), reverse=True)[:3]
                if recent_days:
                    avg_size = sum(daily_sizes[d] for d in recent_days) / len(recent_days)
                    if avg_size > 0 and today_total < avg_size * size_threshold_percent / 100:
                        notification(
                            bucket_name.name,
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


def notification(bucket_name, file_date, file_name, file_size, alert_type="missing", prev_file_name=None, prev_file_size=None):
    if alert_type == "size":
        subject = f"S3 Backup suspicious size ⚠️ {bucket_name}"
        message = (
            f"S3 Backup Notifier\n"
            f"Today's backup total size is abnormally small:\n"
            f"Today: {file_name} — {file_size}\n"
            f"Previous: {prev_file_name} — {prev_file_size}"
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

    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue
        if bucket_name.name in bucket_blacklist:
            continue

        total_count += 1
        name = bucket_name.name

        try:
            bucket = s3.Bucket(name)
            objs = bucket.objects.all()
            root_objs = sorted(
                [obj for obj in objs if '/' not in obj.key],
                key=lambda o: o.last_modified,
                reverse=True
            )

            if not root_objs:
                lines.append(f"❌ `{name}` — empty bucket")
                continue

            today_objs = [obj for obj in root_objs if obj.last_modified.date() == today]

            if not today_objs:
                last = root_objs[0]
                lines.append(f"❌ `{name}` — no backup today (last: {last.last_modified.date()})")
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

            if is_suspicious:
                lines.append(f"⚠️ `{name}` — {file_count} files, {sizeof_fmt(today_total)}{variation}")
            else:
                lines.append(f"✅ `{name}` — {file_count} files, {sizeof_fmt(today_total)}{variation}")
                ok_count += 1

        except botocore.exceptions.ClientError as e:
            lines.append(f"❌ `{name}` — error: {e.response['Error']['Message']}")

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


# Run locally for testing purpose
if __name__ == '__main__':
    main(0, 0)
