import hashlib
import json
import re
import time
from contextlib import closing

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS submission_idempotency(
        username TEXT NOT NULL, endpoint TEXT NOT NULL, idem_key TEXT NOT NULL,
        request_hash TEXT NOT NULL, response_json TEXT, created_at INTEGER, updated_at INTEGER,
        PRIMARY KEY(username, endpoint, idem_key))""")

def clean_key(raw):
    key = str(raw or "").strip()
    if key and not _KEY_RE.fullmatch(key):
        raise ValueError("Idempotency-Key 需为 8-128 位字母、数字或 . _ : -")
    return key

def _request_hash(body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def begin(db_factory, username, endpoint, key, body):
    if not key:
        return "disabled", None
    digest, now = _request_hash(body), int(time.time())
    with closing(db_factory()) as connection:
        ensure_table(connection)
        inserted = connection.execute(
            "INSERT OR IGNORE INTO submission_idempotency"
            "(username,endpoint,idem_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (username, endpoint, key, digest, now, now)).rowcount
        connection.commit()
        if inserted == 1:
            return "new", None
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if not row:
            raise RuntimeError("idempotency claim disappeared")
        if row["request_hash"] != digest:
            return "conflict", None
        return ("replay", json.loads(row["response_json"])) if row["response_json"] else ("processing", None)

def complete(db_factory, username, endpoint, key, response):
    if key:
        with closing(db_factory()) as connection:
            connection.execute(
                "UPDATE submission_idempotency SET response_json=?,updated_at=? WHERE username=? AND endpoint=? AND idem_key=?",
                (json.dumps(response, ensure_ascii=False), int(time.time()), username, endpoint, key))
            connection.commit()

def abort(db_factory, username, endpoint, key):
    if key:
        with closing(db_factory()) as connection:
            connection.execute(
                "DELETE FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=? AND response_json IS NULL",
                (username, endpoint, key))
            connection.commit()
