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
    notification_method = os.environ.get('NOTIFICATION_METHOD', 'email')  # 'email' or 'slack'
    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue
        
        if bucket_name.name in bucket_blacklist:
            continue

        try:
            print("Bucket --> " + str(bucket_name.name))
            bucket = s3.Bucket(bucket_name.name)
            objs = bucket.objects.all()
            
            root_objs = [obj for obj in objs if '/' not in obj.key]
            if not root_objs:
                continue

            backup_success = 0
            file_date = 0
            file_name = ''
            file_size = 0
            for obj in root_objs:
                print(obj.last_modified.date(), obj.key, sizeof_fmt(obj.size))
                file_date = obj.last_modified.date()
                file_name = obj.key
                file_size = sizeof_fmt(obj.size)
                if obj.last_modified.date() == today:
                    print("Backup OK, All Good")
                    print("--> " + str(file_date), file_name, file_size)
                    backup_success = 1
                    
            if backup_success == 0:
                notification(bucket_name.name, file_date=file_date, file_name=str(file_name), file_size=str(file_size))
                print("No backup detected from today: " + str(today))
                print("--> Last backup file: " + str(file_date), file_name, file_size)
                
        except botocore.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            print(e.response['Error']['Message'])
            if error_code == '404':
                print("There is no file in this bucket")
            else:
                print(e)


def notification(bucket_name, file_date, file_name, file_size):
    subject = 'S3 Backup failed ❌ ' + bucket_name
    message = f"S3 Backup Notifier\nLast backup comes from:\nDate: {file_date}\nName: {file_name}\nSize: {file_size}"
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
