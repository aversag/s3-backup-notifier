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

# Get Today's date
today = datetime.date.today()

# AWS Connection
session = boto3.Session(region_name=aws_region)
s3 = session.resource('s3')
bucket_names = s3.buckets.all()

# Classification patterns. First match wins; 'site' is a fallback for archives that
# match none of the above.
COMPONENT_PATTERNS = [
    ('etc',  re.compile(r'(^|[-./_])etc[-.]', re.I)),
    ('boot', re.compile(r'(^|[-./_])boot[-.]', re.I)),
    # s3backup role bundles /boot + /etc into a single '<prefix>-system-<date>.tar.gz'.
    ('system', re.compile(r'(^|[-./_])system[-.]', re.I)),
    # '-db-' covers the s3backup naming for both mysqldump (-db-*.sql.gz) and
    # mydumper (-db-*.mydumper.tar.gz) dumps, on top of the legacy backup-manager patterns.
    ('db',   re.compile(r'(\.sql\.(gz|bz2|xz)$|\.dump$|\.sql$|_gitlab_backup\.tar$|-mysql-|-postgres-|-db-|\.pgdump$)', re.I)),
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
    # A single '-system-' tarball (s3backup) carries both /boot and /etc, so it
    # satisfies legacy 'boot'/'etc' expectations without touching BUCKET_COMPONENTS.
    if 'system' in found:
        found.update({'boot', 'etc'})
    return found


def _list_root_objs(bucket_name):
    """Return root-level objects only, sorted newest first.

    Using Delimiter='/' lets S3 skip everything under sub-prefixes server-side
    instead of streaming millions of keys to the Lambda just to filter them out.
    """
    bucket = s3.Bucket(bucket_name)
    root_objs = list(bucket.objects.filter(Delimiter='/'))
    root_objs.sort(key=lambda o: o.last_modified, reverse=True)
    return root_objs


def main(event, context):
    size_threshold_percent = int(os.environ.get('SIZE_THRESHOLD_PERCENT', '50'))
    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue

        if bucket_name.name in bucket_blacklist:
            continue

        try:
            print("Bucket --> " + str(bucket_name.name))
            root_objs = _list_root_objs(bucket_name.name)
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
                continue

            today_total = sum(obj.size for obj in today_objs)
            print("Backup OK, All Good")
            print(f"--> Today: {len(today_objs)} files, total {sizeof_fmt(today_total)}")

            # Component-level check (only if bucket is configured)
            expected = bucket_components.get(bucket_name.name)
            if expected:
                found = _today_components(today_objs)
                missing = [c for c in expected if c not in found]
                if missing:
                    notification(
                        bucket_name.name,
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

    for bucket_name in bucket_names:
        if not bucket_name.name.startswith(s3_prefix):
            continue
        if bucket_name.name in bucket_blacklist:
            continue

        total_count += 1
        name = bucket_name.name

        try:
            root_objs = _list_root_objs(name)

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

            # Component check
            comp_warning = ""
            expected = bucket_components.get(name)
            if expected:
                found = _today_components(today_objs)
                missing = [c for c in expected if c not in found]
                if missing:
                    comp_warning = f" — missing: {','.join(missing)}"

            if is_suspicious or comp_warning:
                lines.append(f"⚠️ `{name}` — {file_count} files, {sizeof_fmt(today_total)}{variation}{comp_warning}")
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


def _collect_statuses():
    """Scan every monitored backup bucket, return a per-bucket status dict.

    Same logic as report() (root-level objects, 3-day average, component check)
    but returns structured data for HTML rendering instead of Slack text.
    """
    size_threshold_percent = int(os.environ.get('SIZE_THRESHOLD_PERCENT', '50'))
    day = datetime.date.today()  # recompute: a warm container may outlive a day
    statuses = []
    for bucket in s3.buckets.all():
        if not bucket.name.startswith(s3_prefix):
            continue
        if bucket.name in bucket_blacklist:
            continue
        entry = {'name': bucket.name}
        try:
            root_objs = _list_root_objs(bucket.name)
        except botocore.exceptions.ClientError as e:
            entry.update(state='error', detail=e.response['Error']['Message'])
            statuses.append(entry)
            continue
        if not root_objs:
            entry.update(state='fail', detail='bucket vide', last_date=None)
            statuses.append(entry)
            continue

        today_objs = [o for o in root_objs if o.last_modified.date() == day]
        last = root_objs[0]
        daily = {}
        for o in root_objs:
            d = o.last_modified.date()
            if d < day:
                daily[d] = daily.get(d, 0) + o.size
        recent = sorted(daily, reverse=True)[:3]
        avg = sum(daily[d] for d in recent) / len(recent) if recent else 0
        today_total = sum(o.size for o in today_objs)
        variation = ((today_total - avg) / avg * 100) if avg > 0 else None
        suspicious = bool(recent) and avg > 0 and today_total < avg * size_threshold_percent / 100
        expected = bucket_components.get(bucket.name)
        missing = [c for c in expected if c not in _today_components(today_objs)] if expected else []

        if not today_objs:
            state = 'fail'
        elif suspicious or missing:
            state = 'warn'
        else:
            state = 'ok'
        entry.update(
            state=state,
            last_date=last.last_modified.date().isoformat(),
            today_count=len(today_objs),
            today_size_h=sizeof_fmt(today_total),
            avg_size_h=(sizeof_fmt(avg) if avg else '—'),
            variation=(round(variation) if variation is not None else None),
            missing=missing,
        )
        statuses.append(entry)

    order = {'fail': 0, 'error': 0, 'warn': 1, 'ok': 2}
    statuses.sort(key=lambda s: (order.get(s['state'], 3), s['name']))
    return statuses


def _render_dashboard(statuses):
    import html as _html
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    day = datetime.date.today().isoformat()
    total = len(statuses)
    ok = sum(1 for s in statuses if s['state'] == 'ok')
    warn = sum(1 for s in statuses if s['state'] == 'warn')
    bad = sum(1 for s in statuses if s['state'] in ('fail', 'error'))
    colors = {'ok': '#22c55e', 'warn': '#f59e0b', 'fail': '#ef4444', 'error': '#ef4444'}
    labels = {'ok': 'OK', 'warn': 'ATTENTION', 'fail': 'ÉCHEC', 'error': 'ERREUR'}

    cards = []
    for s in statuses:
        c = colors.get(s['state'], '#64748b')
        lab = labels.get(s['state'], s['state'])
        name = _html.escape(s['name'])
        if s['state'] == 'error':
            body = "<div class='detail'>%s</div>" % _html.escape(s.get('detail', ''))
        elif s.get('last_date') is None:
            body = "<div class='detail'>bucket vide</div>"
        else:
            var = s.get('variation')
            var_html = ""
            if var is not None:
                var_html = "<span class='var'>%s %+d%%</span>" % ('↑' if var >= 0 else '↓', var)
            miss = ""
            if s.get('missing'):
                miss = "<div class='miss'>manque : %s</div>" % _html.escape(', '.join(s['missing']))
            fresh = "backup du jour" if s['state'] != 'fail' else ("dernier : %s" % s.get('last_date'))
            body = (
                "<div class='row'><span>%s fichiers</span><span class='size'>%s%s</span></div>"
                "<div class='row sub'><span>%s</span><span>moy 3j : %s</span></div>%s"
                % (s.get('today_count', 0), _html.escape(str(s.get('today_size_h', '—'))), var_html,
                   fresh, _html.escape(str(s.get('avg_size_h', '—'))), miss)
            )
        cards.append(
            "<div class='card' style='border-left:4px solid %s'>"
            "<div class='hd'><span class='name'>%s</span>"
            "<span class='badge' style='background:%s'>%s</span></div>%s</div>"
            % (c, name, c, lab, body)
        )
    cards_html = "\n".join(cards)

    return """<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Backups — {ok}/{total} OK</title>
<style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0}}
header{{padding:20px 24px;border-bottom:1px solid #1e293b;display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}}
h1{{margin:0;font-size:18px;font-weight:600}}
.sum{{display:flex;gap:10px;font-size:13px}}
.pill{{padding:2px 10px;border-radius:999px;font-weight:600;color:#0f172a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:20px 24px}}
.card{{background:#1e293b;border-radius:10px;padding:14px 16px}}
.hd{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}}
.name{{font-weight:600;word-break:break-all}}
.badge{{color:#0f172a;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap}}
.row{{display:flex;justify-content:space-between;gap:8px}}
.row.sub{{color:#94a3b8;font-size:12px;margin-top:2px}}
.size{{font-variant-numeric:tabular-nums}}
.var{{color:#94a3b8;font-size:12px;margin-left:6px}}
.miss{{margin-top:6px;color:#f59e0b;font-size:12px}}
.detail{{color:#94a3b8}}
footer{{padding:12px 24px;color:#64748b;font-size:12px;border-top:1px solid #1e293b}}
</style></head><body>
<header>
<h1>🗄️ Backups Will-Hosting</h1>
<div class="sum">
<span class="pill" style="background:#22c55e"><b>{ok}</b> OK</span>
<span class="pill" style="background:#f59e0b"><b>{warn}</b> attention</span>
<span class="pill" style="background:#ef4444"><b>{bad}</b> échec</span>
</div>
</header>
<div class="grid">
{cards_html}
</div>
<footer>Généré le {now} · {total} buckets · rafraîchissement auto 5 min · jour de référence {day}</footer>
</body></html>""".format(ok=ok, total=total, warn=warn, bad=bad, cards_html=cards_html, now=now, day=day)


def dashboard_http(event, context):
    """Lambda Function URL handler: private HTML dashboard of all backups.

    Two independent, composable gates (defense in depth):
      * DASHBOARD_ALLOWED_IPS — comma-separated CIDRs; reject other source IPs.
      * DASHBOARD_AUTH         — "user:password" HTTP Basic Auth.
    Enforced only if set; if BOTH are empty the dashboard is fail-closed (503).
    """
    import base64
    import ipaddress
    auth = os.environ.get('DASHBOARD_AUTH', '')
    allowed = os.environ.get('DASHBOARD_ALLOWED_IPS', '')
    if not auth and not allowed:
        return {'statusCode': 503,
                'headers': {'Content-Type': 'text/plain; charset=utf-8'},
                'body': 'Dashboard non protege : definir DASHBOARD_ALLOWED_IPS et/ou DASHBOARD_AUTH.'}

    # 1) IP allow-list (if configured). Function URL payload v2 -> requestContext.http.sourceIp
    if allowed:
        src = ((event.get('requestContext') or {}).get('http') or {}).get('sourceIp', '')
        permitted = False
        try:
            ip = ipaddress.ip_address(src)
            permitted = any(ip in ipaddress.ip_network(c.strip(), strict=False)
                            for c in allowed.split(',') if c.strip())
        except ValueError:
            permitted = False
        if not permitted:
            print("dashboard: IP refusee:", src)
            return {'statusCode': 403, 'headers': {'Content-Type': 'text/plain; charset=utf-8'},
                    'body': 'IP non autorisee.'}

    # 2) HTTP Basic Auth (if configured)
    if auth:
        hdrs = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
        provided = hdrs.get('authorization', '')
        ok = False
        if provided.startswith('Basic '):
            try:
                ok = base64.b64decode(provided[6:]).decode('utf-8', 'replace') == auth
            except Exception:
                ok = False
        if not ok:
            return {'statusCode': 401,
                    'headers': {'WWW-Authenticate': 'Basic realm="Backups"', 'Content-Type': 'text/plain; charset=utf-8'},
                    'body': 'Authentification requise.'}
    try:
        return {'statusCode': 200,
                'headers': {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'},
                'body': _render_dashboard(_collect_statuses())}
    except Exception as e:  # never leak a stack trace to the browser
        print("dashboard error:", repr(e))
        return {'statusCode': 500, 'headers': {'Content-Type': 'text/plain; charset=utf-8'},
                'body': 'Erreur interne lors de la generation du dashboard.'}


# Run locally for testing purpose
if __name__ == '__main__':
    main(0, 0)
