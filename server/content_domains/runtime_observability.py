"""Private operational evidence and durable, opt-in health notification outbox."""
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path


def database():
    path = Path(os.environ.get('HQ_OBSERVABILITY_DB', str(Path(__file__).resolve().parents[1] / 'runtime_observability.db')))
    connection = sqlite3.connect(str(path), timeout=3)
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS task_trace(
          job_id TEXT, stage TEXT, state TEXT, started REAL, updated REAL,
          duration REAL, metadata TEXT, PRIMARY KEY(job_id,stage));
        CREATE TABLE IF NOT EXISTS alert_outbox(
          event_id TEXT PRIMARY KEY, payload TEXT, state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, updated REAL,
          error TEXT DEFAULT '');
    ''')
    return connection


def record(job_id, stage, state, duration=None, **metadata):
    if not job_id:
        return
    allowed = {'provider', 'model', 'host', 'transport', 'provider_task_id', 'error_type'}
    data = {key: str(value)[:160] for key, value in metadata.items() if key in allowed}
    try:
        with closing(database()) as connection:
            now = time.time()
            connection.execute('''INSERT INTO task_trace VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id,stage) DO UPDATE SET state=excluded.state,
                updated=excluded.updated,duration=excluded.duration,metadata=excluded.metadata''',
                (str(job_id), stage, state, now, now, duration, json.dumps(data)))
            connection.commit()
    except (OSError, sqlite3.Error):
        # Missing evidence must not alter the paid task outcome or claim success.
        pass


def call(job_id, stage, action, **metadata):
    started = time.monotonic()
    record(job_id, stage, 'running', **metadata)
    try:
        result = action()
    except Exception as exc:
        rejected = type(exc).__name__ == 'MiniMaxCredentialRejected' or getattr(exc,'definitive_rejection',False)
        state = 'unknown' if stage == 'provider_submit' and not rejected else 'failed'
        record(job_id, stage, state, time.monotonic()-started,
               error_type=type(exc).__name__, **metadata)
        raise
    record(job_id, stage, 'recorded', time.monotonic()-started, **metadata)
    return result


def traces(job_id):
    try:
        with closing(database()) as connection:
            rows = connection.execute('SELECT * FROM task_trace WHERE job_id=? ORDER BY started', (str(job_id),)).fetchall()
        return [{'stage': row['stage'], 'state': row['state'], 'started_at': row['started'],
                 'updated_at': row['updated'], 'duration_sec': row['duration'],
                 **json.loads(row['metadata'])} for row in rows]
    except (OSError, sqlite3.Error):
        return []


def enqueue(action, service, occurred_at):
    # Deliberately exclude raw probe details, credentials, user data and task output.
    payload = {'event': action, 'service': service, 'occurred_at': occurred_at}
    event_id = '%s:%s:%s' % (action, service, occurred_at)
    with closing(database()) as connection:
        connection.execute('INSERT OR IGNORE INTO alert_outbox(event_id,payload,updated) VALUES(?,?,?)',
                           (event_id, json.dumps(payload), time.time()))
        connection.commit()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_endpoint(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password or not parsed.hostname:
            return False
        return parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})
    except ValueError:
        return False


def dispatch():
    url = os.environ.get('HQ_ALERT_WEBHOOK_URL', '').strip()
    base_enabled = os.environ.get('HQ_ALERT_ENABLED') == '1' and valid_endpoint(url)
    from . import channel_manager
    try:
        channel_settings = channel_manager.notification_settings(True)
    except Exception:
        channel_settings = {}
    channel_url = channel_settings.get('endpoint','') if channel_settings.get('enabled') else ''
    if not base_enabled and not channel_url:
        return
    now = time.time()
    with closing(database()) as connection:
        rows = connection.execute("SELECT * FROM alert_outbox WHERE state='pending' AND next_try<=? ORDER BY updated", (now,)).fetchall()
        dispatched = 0
        for row in rows:
            is_channel = str(json.loads(row['payload']).get('event','')).startswith('channel.')
            target = channel_url if is_channel else url if base_enabled else ''
            if not target:
                continue
            if dispatched >= 5:
                break
            dispatched += 1
            attempts = row['attempts']+1
            try:
                request = urllib.request.Request(target, row['payload'].encode(), {'Content-Type':'application/json', 'Idempotency-Key':row['event_id']}, method='POST')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=3) as response:
                    if not 200 <= response.status < 300:
                        raise OSError('notification rejected')
                state, error = 'sent', ''
            except Exception as exc:
                state, error = ('failed' if attempts >= 5 else 'pending'), type(exc).__name__
            connection.execute('UPDATE alert_outbox SET state=?,attempts=?,next_try=?,updated=?,error=? WHERE event_id=?',
                               (state, attempts, now+min(3600, 60*2**attempts), now, error, row['event_id']))
            connection.commit()
        connection.commit()


def alert_status():
    requested = os.environ.get('HQ_ALERT_ENABLED') == '1'
    enabled = requested and valid_endpoint(os.environ.get('HQ_ALERT_WEBHOOK_URL', ''))
    try:
        with closing(database()) as connection:
            counts = {r['state']:r['n'] for r in connection.execute('SELECT state,COUNT(*) n FROM alert_outbox GROUP BY state')}
        return {'enabled':enabled, 'counts':counts, 'error':'通知地址配置无效' if requested and not enabled else ''}
    except (OSError, sqlite3.Error):
        return {'enabled':enabled, 'error':'通知记录不可读'}
