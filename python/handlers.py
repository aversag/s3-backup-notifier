import datetime
import boto3
import botocore
import os
from botocore.exceptions import ClientError

# Config
bucket_blacklist = os.environ['BUCKETSBLACKLIST'].split(",")
s3_prefix = os.environ['S3PREFIX']
recipients = os.environ['RECIPIENTS'].split(",")
sender = "S3 Backup Notifier <" + os.environ['SENDER'] + ">"
aws_region = os.environ['AWSREGION']
if 'AWSSESREGION' in os.environ:
    aws_ses_region = os.environ['AWSSESREGION']
else:
    aws_ses_region = os.environ['AWSREGION']
    
# Get Today's date
today = datetime.date.today()

# AWS Connection
session = boto3.Session(region_name=aws_region)
s3 = session.resource('s3')
ses = session.client('ses', region_name=aws_ses_region)
bucket_names = s3.buckets.all()


# Convert to Human Readable
def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def main(event, context):
    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue
        
        if bucket_name.name in bucket_blacklist:
            continue

        try:
            print("Bucket --> " + str(bucket_name.name))
            bucket = s3.Bucket(bucket_name.name)
            objs = bucket.objects.all()
            
            if not objs:
                continue

            backup_success = 0
            file_date = 0
            file_name = ''
            file_size = 0
            
            for obj in objs:
                
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
    try:
        subject = 'S3 Backup failed ❌' + bucket_name
        CHARSET = "UTF-8"
        # Email body for recipients with non-HTML email clients.
        BODY_TEXT = ("S3 Backup Notifier\r\n"
                    "Last backup comes from:\r\n"
                    """str(file_date), file_name, file_size""""\r\n"
                    "S3 Backup Notifier"
                    )
        # HTML body of the email.
        BODY_HTML = """
        <html>
            <body>
                <h1>S3 Backup Notifier 👨‍🚒</h1>
                <h3>Last backup comes from:</h3>
                <table cellpadding="4" cellspacing="4" border="1">
                <tr><td>Date</td><td>Name</td><td>Size</td></tr>
                <tr><td>""" + str(file_date) + """</td><td>""" + file_name + """</td><td>""" + file_size + """</td></tr>
                </table>
                <p><a href="https://github.com/z0ph/s3-backup-notifier">S3 Backup Notifier</a></p>
            </body>
        </html>
                    """

        # Provide the contents of the email.
        response = ses.send_email(
            Destination={
                'ToAddresses': recipients
            },
            Message={
                'Body': {
                    'Html': {
                        'Charset': CHARSET,
                        'Data': BODY_HTML,
                    },
                    'Text': {
                        'Charset': CHARSET,
                        'Data': BODY_TEXT,
                    },
                },
                'Subject': {
                    'Charset': CHARSET,
                    'Data': subject
                },
            },
            Source=sender,
        )
    # Display an error if something goes wrong.
    except ClientError as e:
        print(e.response['Error']['Message'])
    else:
        print("Email sent! Message ID:"),
        print(response['MessageId'])


# Run locally for testing purpose
if __name__ == '__main__':
    main(0, 0)
