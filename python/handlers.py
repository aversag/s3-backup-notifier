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
            previous_objs = [obj for obj in root_objs if obj.last_modified.date() < today]

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
                today_obj = today_objs[0]
                print("Backup OK, All Good")
                print("--> " + str(today_obj.last_modified.date()), today_obj.key, sizeof_fmt(today_obj.size))

                if previous_objs:
                    prev_obj = previous_objs[0]
                    if prev_obj.size > 0 and today_obj.size < prev_obj.size * size_threshold_percent / 100:
                        notification(
                            bucket_name.name,
                            file_date=today_obj.last_modified.date(),
                            file_name=today_obj.key,
                            file_size=sizeof_fmt(today_obj.size),
                            alert_type="size",
                            prev_file_name=prev_obj.key,
                            prev_file_size=sizeof_fmt(prev_obj.size)
                        )
                        print(f"Size alert: {sizeof_fmt(today_obj.size)} vs previous {sizeof_fmt(prev_obj.size)}")

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
            f"Today's backup is abnormally small:\n"
            f"Today: {file_name} ({file_size})\n"
            f"Previous: {prev_file_name} ({prev_file_size})"
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


# Run locally for testing purpose
if __name__ == '__main__':
    main(0, 0)
