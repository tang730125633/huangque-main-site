"""SQLite persistence for the independent AI Creator workspace."""

from __future__ import annotations

from contextlib import closing
import json
import pathlib
import sqlite3
import time
import uuid


class StoreError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed


class CreatorAgentStore:
    """Project-scoped state; IP12 itself remains the profile source of truth."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def db(self):
        connection = sqlite3.connect(str(self.path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    def init_schema(self):
        with closing(self.db()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            legacy_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(creator_messages)")
            }
            if legacy_columns and "project_id" not in legacy_columns:
                suffix = 1
                archive = "creator_messages_v1_archive"
                existing_tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                while archive in existing_tables:
                    suffix += 1
                    archive = "creator_messages_v1_archive_%s" % suffix
                connection.execute(
                    "ALTER TABLE creator_messages RENAME TO %s" % archive
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS creator_account_state(
                    username TEXT PRIMARY KEY,
                    active_project_id TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creator_workspaces(
                    username TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    alias TEXT NOT NULL DEFAULT '',
                    platforms_json TEXT NOT NULL DEFAULT '[]',
                    preferences_json TEXT NOT NULL DEFAULT '{}',
                    profile_overrides_json TEXT NOT NULL DEFAULT '{}',
                    flow_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(username, project_id)
                );
                CREATE INDEX IF NOT EXISTS idx_creator_workspaces_user
                    ON creator_workspaces(username, updated_at DESC);
                CREATE TABLE IF NOT EXISTS creator_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_key TEXT,
                    request_id TEXT,
                    public_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(username, project_id)
                        REFERENCES creator_workspaces(username, project_id) ON DELETE CASCADE,
                    UNIQUE(username, project_id, source_key),
                    UNIQUE(username, project_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_creator_messages_project
                    ON creator_messages(username, project_id, id);
                CREATE TABLE IF NOT EXISTS creator_batches(
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    confirmation_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(username, project_id)
                        REFERENCES creator_workspaces(username, project_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_creator_batches_project
                    ON creator_batches(username, project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS creator_jobs(
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    quote_token TEXT NOT NULL DEFAULT '',
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL,
                    confirmation_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    refund_status TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES creator_batches(id) ON DELETE CASCADE,
                    UNIQUE(batch_id, platform)
                );
                CREATE INDEX IF NOT EXISTS idx_creator_jobs_project
                    ON creator_jobs(username, project_id, created_at DESC);
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(creator_workspaces)")
            }
            if "profile_overrides_json" not in columns:
                connection.execute(
                    "ALTER TABLE creator_workspaces ADD COLUMN profile_overrides_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.commit()

    def health(self):
        """Verify integrity and a rollback-only write without persisting a sentinel."""
        try:
            with closing(self.db()) as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    return False
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO creator_account_state(username,active_project_id,updated_at)
                       VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET
                       updated_at=excluded.updated_at""",
                    ("__creator_health__", "", int(time.time())),
                )
                connection.rollback()
            return True
        except (OSError, sqlite3.Error):
            return False

    def set_active_project(self, username, project_id):
        now = int(time.time())
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO creator_account_state(username,active_project_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET
                   active_project_id=excluded.active_project_id,updated_at=excluded.updated_at""",
                (username, project_id, now),
            )
            connection.commit()

    def active_project(self, username):
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT active_project_id FROM creator_account_state WHERE username=?",
                (username,),
            ).fetchone()
        return str(row["active_project_id"] or "") if row else ""

    def ensure_workspace(self, username, project_id, alias=""):
        now = int(time.time())
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO creator_workspaces
                   (username,project_id,alias,created_at,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(username,project_id) DO NOTHING""",
                (username, project_id, str(alias or "")[:120], now, now),
            )
            connection.commit()
        return self.workspace(username, project_id)

    @staticmethod
    def _workspace(row):
        if not row:
            return None
        preferences = _loads(row["preferences_json"], {})
        if not isinstance(preferences, dict):
            preferences = {}
        preferences.setdefault("global", [])
        preferences.setdefault("platforms", {})
        overrides = _loads(row["profile_overrides_json"], {})
        if not isinstance(overrides, dict):
            overrides = {}
        return {
            "project_id": row["project_id"],
            "alias": row["alias"],
            "platforms": _loads(row["platforms_json"], []),
            "template_video_preferences": preferences,
            "profile_overrides": overrides,
            "flow": _loads(row["flow_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def workspace(self, username, project_id):
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT * FROM creator_workspaces WHERE username=? AND project_id=?",
                (username, project_id),
            ).fetchone()
        return self._workspace(row)

    def update_workspace(self, username, project_id, **changes):
        mapping = {
            "alias": "alias",
            "platforms": "platforms_json",
            "template_video_preferences": "preferences_json",
            "profile_overrides": "profile_overrides_json",
            "flow": "flow_json",
        }
        if not changes or set(changes) - set(mapping):
            raise StoreError("invalid workspace update")
        values = {}
        for key, value in changes.items():
            values[mapping[key]] = str(value or "")[:120] if key == "alias" else _json(value)
        values["updated_at"] = int(time.time())
        fields = ",".join("%s=?" % key for key in values)
        with closing(self.db()) as connection:
            changed = connection.execute(
                "UPDATE creator_workspaces SET %s WHERE username=? AND project_id=?" % fields,
                tuple(values.values()) + (username, project_id),
            )
            if changed.rowcount != 1:
                raise StoreError("workspace not found")
            connection.commit()
        return self.workspace(username, project_id)

    @staticmethod
    def _message(row):
        if not row:
            return None
        return {
            "id": row["id"], "role": row["role"], "content": row["content"],
            "public": _loads(row["public_json"], {}), "created_at": row["created_at"],
        }

    def add_message(self, username, project_id, role, content, *, source_key=None,
                    request_id=None, public=None, created_at=None):
        now = int(created_at or time.time())
        with closing(self.db()) as connection:
            if not connection.execute(
                "SELECT 1 FROM creator_workspaces WHERE username=? AND project_id=?",
                (username, project_id),
            ).fetchone():
                raise StoreError("workspace not found")
            try:
                cursor = connection.execute(
                    """INSERT INTO creator_messages
                       (username,project_id,role,content,source_key,request_id,public_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (username, project_id, role, content, source_key, request_id,
                     _json(public or {}), now),
                )
            except sqlite3.IntegrityError:
                if request_id:
                    row = connection.execute(
                        """SELECT * FROM creator_messages
                           WHERE username=? AND project_id=? AND request_id=?""",
                        (username, project_id, request_id),
                    ).fetchone()
                elif source_key:
                    row = connection.execute(
                        """SELECT * FROM creator_messages
                           WHERE username=? AND project_id=? AND source_key=?""",
                        (username, project_id, source_key),
                    ).fetchone()
                else:
                    raise
                return self._message(row), False
            connection.execute(
                "UPDATE creator_workspaces SET updated_at=? WHERE username=? AND project_id=?",
                (now, username, project_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM creator_messages WHERE id=?", (cursor.lastrowid,),
            ).fetchone()
        return self._message(row), True

    def messages(self, username, project_id, limit=300):
        with closing(self.db()) as connection:
            rows = connection.execute(
                """SELECT * FROM creator_messages WHERE username=? AND project_id=?
                   ORDER BY id DESC LIMIT ?""",
                (username, project_id, max(1, min(800, int(limit)))),
            ).fetchall()
        return [self._message(row) for row in reversed(rows)]

    def update_message_public(self, username, message_id, public):
        with closing(self.db()) as connection:
            changed = connection.execute(
                "UPDATE creator_messages SET public_json=? WHERE id=? AND username=?",
                (_json(public or {}), int(message_id), username),
            )
            if changed.rowcount != 1:
                raise StoreError("message not found")
            connection.commit()

    def sync_ip12_messages(self, username, project_id, messages):
        with closing(self.db()) as connection:
            existing_rows = connection.execute(
                """SELECT role,content,COUNT(*) AS count FROM creator_messages
                   WHERE username=? AND project_id=? GROUP BY role,content""",
                (username, project_id),
            ).fetchall()
        existing = {(row["role"], row["content"]): int(row["count"]) for row in existing_rows}
        consumed = {}
        added = 0
        for index, item in enumerate(messages or []):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            pair = (item["role"], content)
            used = consumed.get(pair, 0)
            if used < existing.get(pair, 0):
                consumed[pair] = used + 1
                continue
            stable = str(item.get("message_id") or "%s-%s" % (index, uuid.uuid5(
                uuid.NAMESPACE_URL, item["role"] + "\n" + content,
            ).hex))
            _, created = self.add_message(
                username, project_id, item["role"], content,
                source_key="ip12:" + stable[:180],
                public={"source": "ip12", "agent_trace": item.get("agent_trace") or {}},
            )
            added += int(created)
            consumed[pair] = used + 1
        return added

    @staticmethod
    def _job(row, include_private=False):
        if not row:
            return None
        value = {
            "id": row["id"], "batch_id": row["batch_id"],
            "platform": row["platform"], "version": row["version"],
            "status": row["status"], "input": _loads(row["input_json"], {}),
            "quote": _loads(row["quote_json"], {}),
            "result": _loads(row["result_json"], {}), "error": row["error"],
            "refund_status": row["refund_status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if include_private:
            value.update({
                "quote_token": row["quote_token"],
                "idempotency_key": row["idempotency_key"],
                "confirmation_id": row["confirmation_id"],
                "job_id": row["job_id"],
            })
        return value

    def create_batch(self, username, project_id, topic, goal, platform_plans):
        if not platform_plans:
            raise StoreError("platform plans are required")
        now = int(time.time())
        batch_id = "creator_batch_" + uuid.uuid4().hex
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO creator_batches
                   (id,username,project_id,topic,goal,status,plan_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (batch_id, username, project_id, topic, goal, "draft",
                 _json(platform_plans), now, now),
            )
            for plan in platform_plans:
                platform = str(plan.get("platform") or "")
                previous = connection.execute(
                    """SELECT COALESCE(MAX(version),0) FROM creator_jobs
                       WHERE username=? AND project_id=? AND platform=?
                         AND status NOT IN ('draft','ready','quoted','failed_submission')""",
                    (username, project_id, platform),
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO creator_jobs
                       (id,batch_id,username,project_id,platform,version,status,input_json,
                        idempotency_key,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ("creator_job_" + uuid.uuid4().hex, batch_id, username, project_id,
                     platform, int(previous) + 1, "draft", _json(plan.get("input") or {}),
                     "creator-agent-" + uuid.uuid4().hex, now, now),
                )
            connection.commit()
        return self.batch(username, batch_id, include_private=True)

    @staticmethod
    def _batch_row(row):
        if not row:
            return None
        return {
            "id": row["id"], "project_id": row["project_id"],
            "topic": row["topic"], "goal": row["goal"], "status": row["status"],
            "plans": _loads(row["plan_json"], []), "quote": _loads(row["quote_json"], {}),
            "confirmation_id": row["confirmation_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def batch(self, username, batch_id, include_private=False):
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT * FROM creator_batches WHERE id=? AND username=?",
                (batch_id, username),
            ).fetchone()
            jobs = connection.execute(
                "SELECT * FROM creator_jobs WHERE batch_id=? AND username=? ORDER BY platform",
                (batch_id, username),
            ).fetchall() if row else []
        value = self._batch_row(row)
        if value is not None:
            value["jobs"] = [self._job(item, include_private) for item in jobs]
            if not include_private:
                value.pop("confirmation_id", None)
        return value

    def batches(self, username, project_id, limit=30):
        with closing(self.db()) as connection:
            rows = connection.execute(
                """SELECT id FROM creator_batches WHERE username=? AND project_id=?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (username, project_id, max(1, min(100, int(limit)))),
            ).fetchall()
        return [self.batch(username, row["id"]) for row in rows]

    def latest_batch(self, username, project_id, include_private=False):
        with closing(self.db()) as connection:
            row = connection.execute(
                """SELECT id FROM creator_batches WHERE username=? AND project_id=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (username, project_id),
            ).fetchone()
        return self.batch(username, row["id"], include_private) if row else None

    def update_batch(self, username, batch_id, **changes):
        mapping = {"status": "status", "plans": "plan_json", "quote": "quote_json",
                   "confirmation_id": "confirmation_id", "goal": "goal", "topic": "topic"}
        if not changes or set(changes) - set(mapping):
            raise StoreError("invalid batch update")
        values = {}
        for key, value in changes.items():
            values[mapping[key]] = _json(value) if key in {"plans", "quote"} else value
        values["updated_at"] = int(time.time())
        fields = ",".join("%s=?" % key for key in values)
        with closing(self.db()) as connection:
            changed = connection.execute(
                "UPDATE creator_batches SET %s WHERE id=? AND username=?" % fields,
                tuple(values.values()) + (batch_id, username),
            )
            if changed.rowcount != 1:
                raise StoreError("batch not found")
            connection.commit()
        return self.batch(username, batch_id, include_private=True)

    def update_job(self, username, record_id, **changes):
        mapping = {
            "status": "status", "input": "input_json", "quote_token": "quote_token",
            "quote": "quote_json", "confirmation_id": "confirmation_id",
            "job_id": "job_id", "result": "result_json", "error": "error",
            "refund_status": "refund_status",
        }
        if not changes or set(changes) - set(mapping):
            raise StoreError("invalid job update")
        values = {}
        for key, value in changes.items():
            values[mapping[key]] = _json(value) if key in {"input", "quote", "result"} else value
        values["updated_at"] = int(time.time())
        fields = ",".join("%s=?" % key for key in values)
        with closing(self.db()) as connection:
            changed = connection.execute(
                "UPDATE creator_jobs SET %s WHERE id=? AND username=?" % fields,
                tuple(values.values()) + (record_id, username),
            )
            if changed.rowcount != 1:
                raise StoreError("job not found")
            connection.commit()
            row = connection.execute(
                "SELECT * FROM creator_jobs WHERE id=? AND username=?", (record_id, username),
            ).fetchone()
        return self._job(row, include_private=True)

    def replace_batch_plans(self, username, batch_id, platform_plans):
        batch = self.batch(username, batch_id, include_private=True)
        if not batch:
            raise StoreError("batch not found")
        if batch["status"] not in {"draft", "ready", "quoted"}:
            raise StoreError("batch is not editable")
        incoming = {str(item.get("platform") or ""): item for item in platform_plans}
        if set(incoming) != {job["platform"] for job in batch["jobs"]}:
            raise StoreError("platform set cannot change during revision")
        for job in batch["jobs"]:
            self.update_job(
                username, job["id"], status="draft",
                input=incoming[job["platform"]].get("input") or {},
                quote_token="", quote={}, error="",
            )
        return self.update_batch(username, batch_id, status="ready", plans=platform_plans, quote={})
