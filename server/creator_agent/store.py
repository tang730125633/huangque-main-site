"""SQLite persistence for the independent AI Creator workspace."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import pathlib
import sqlite3
import time
import uuid


class StoreError(RuntimeError):
    pass


class StateConflict(StoreError):
    pass


class IdempotencyConflict(StoreError):
    pass


class QuoteExpired(StoreError):
    pass


STALE_CLAIM_SECONDS = 120


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _loads(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed


class CreatorAgentStore:
    """Project-scoped state for the independent Creator profile and productions."""

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
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    profile_state_json TEXT NOT NULL DEFAULT '{}',
                    deliverables_json TEXT NOT NULL DEFAULT '{}',
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
                    request_hash TEXT NOT NULL DEFAULT '',
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
                    revision INTEGER NOT NULL DEFAULT 1,
                    plan_hash TEXT NOT NULL DEFAULT '',
                    quoted_revision INTEGER NOT NULL DEFAULT 0,
                    quote_expires_at INTEGER NOT NULL DEFAULT 0,
                    claim_id TEXT NOT NULL DEFAULT '',
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
                    input_hash TEXT NOT NULL DEFAULT '',
                    quote_token TEXT NOT NULL DEFAULT '',
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    quote_cost INTEGER NOT NULL DEFAULT 0,
                    quote_expires_at INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    submit_input_json TEXT NOT NULL DEFAULT '{}',
                    submit_input_hash TEXT NOT NULL DEFAULT '',
                    submit_quote_token TEXT NOT NULL DEFAULT '',
                    submit_quote_cost INTEGER NOT NULL DEFAULT 0,
                    submit_quote_expires_at INTEGER NOT NULL DEFAULT 0,
                    submit_idempotency_key TEXT NOT NULL DEFAULT '',
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
            for column in ("profile_json", "profile_state_json", "deliverables_json"):
                if column not in columns:
                    connection.execute(
                        "ALTER TABLE creator_workspaces ADD COLUMN %s TEXT NOT NULL DEFAULT '{}'" % column
                    )
            migrations = {
                "creator_messages": {
                    "request_hash": "TEXT NOT NULL DEFAULT ''",
                },
                "creator_batches": {
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                    "plan_hash": "TEXT NOT NULL DEFAULT ''",
                    "quoted_revision": "INTEGER NOT NULL DEFAULT 0",
                    "quote_expires_at": "INTEGER NOT NULL DEFAULT 0",
                    "claim_id": "TEXT NOT NULL DEFAULT ''",
                },
                "creator_jobs": {
                    "input_hash": "TEXT NOT NULL DEFAULT ''",
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                    "submit_input_json": "TEXT NOT NULL DEFAULT '{}'",
                    "submit_input_hash": "TEXT NOT NULL DEFAULT ''",
                    "submit_quote_token": "TEXT NOT NULL DEFAULT ''",
                    "quote_cost": "INTEGER NOT NULL DEFAULT 0",
                    "quote_expires_at": "INTEGER NOT NULL DEFAULT 0",
                    "submit_quote_cost": "INTEGER NOT NULL DEFAULT 0",
                    "submit_quote_expires_at": "INTEGER NOT NULL DEFAULT 0",
                    "submit_idempotency_key": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, definitions in migrations.items():
                existing = {
                    row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)
                }
                for column, definition in definitions.items():
                    if column not in existing:
                        connection.execute(
                            "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition)
                        )
            for row in connection.execute(
                "SELECT id,input_json FROM creator_jobs WHERE input_hash=''"
            ).fetchall():
                connection.execute(
                    "UPDATE creator_jobs SET input_hash=? WHERE id=?",
                    (_digest(_loads(row["input_json"], {})), row["id"]),
                )
            for row in connection.execute(
                "SELECT id,plan_json FROM creator_batches WHERE plan_hash=''"
            ).fetchall():
                connection.execute(
                    "UPDATE creator_batches SET plan_hash=? WHERE id=?",
                    (_digest(_loads(row["plan_json"], [])), row["id"]),
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
            "profile": _loads(row["profile_json"], {}),
            "profile_state": _loads(row["profile_state_json"], {}),
            "deliverables": _loads(row["deliverables_json"], {}),
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
            "profile": "profile_json",
            "profile_state": "profile_state_json",
            "deliverables": "deliverables_json",
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

    def workspaces(self, username):
        with closing(self.db()) as connection:
            rows = connection.execute(
                "SELECT * FROM creator_workspaces WHERE username=? "
                "ORDER BY updated_at DESC,created_at ASC",
                (username,),
            ).fetchall()
        return [self._workspace(row) for row in rows]

    def update_profile_state(self, username, project_id, state, expected_revision,
                             *, profile=None, profile_overrides=None,
                             deliverables=None, flow=None):
        now = int(time.time())
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT profile_state_json FROM creator_workspaces "
                "WHERE username=? AND project_id=?",
                (username, project_id),
            ).fetchone()
            if not row:
                connection.rollback()
                raise StoreError("workspace not found")
            current = _loads(row["profile_state_json"], {})
            if int(current.get("revision") or 1) != int(expected_revision):
                connection.rollback()
                raise StateConflict("profile revision changed")
            if int(state.get("revision") or 0) != int(expected_revision) + 1:
                connection.rollback()
                raise StateConflict("profile revision did not advance exactly once")
            assignments = ["profile_state_json=?", "updated_at=?"]
            values = [_json(state), now]
            for column, value in (
                ("profile_json", profile),
                ("profile_overrides_json", profile_overrides),
                ("deliverables_json", deliverables),
                ("flow_json", flow),
            ):
                if value is not None:
                    assignments.append(column + "=?")
                    values.append(_json(value))
            changed = connection.execute(
                "UPDATE creator_workspaces SET %s WHERE username=? AND project_id=?" %
                ",".join(assignments),
                tuple(values) + (username, project_id),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise StateConflict("profile update lost")
            connection.commit()
        return self.workspace(username, project_id)

    def commit_profile_turn(self, username, project_id, user_message_id,
                            state, expected_revision, reply, public,
                            *, profile=None, profile_overrides=None,
                            deliverables=None, flow=None,
                            fault_hook=None):
        now = int(time.time())
        with closing(self.db()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT profile_state_json FROM creator_workspaces "
                    "WHERE username=? AND project_id=?",
                    (username, project_id),
                ).fetchone()
                if not row:
                    raise StoreError("workspace not found")
                current = _loads(row["profile_state_json"], {})
                if int(current.get("revision") or 1) != int(expected_revision):
                    raise StateConflict("profile revision changed")
                if int(state.get("revision") or 0) != int(expected_revision) + 1:
                    raise StateConflict("profile revision did not advance exactly once")
                assignments = ["profile_state_json=?", "updated_at=?"]
                values = [_json(state), now]
                for column, value in (
                    ("profile_json", profile),
                    ("profile_overrides_json", profile_overrides),
                    ("deliverables_json", deliverables),
                    ("flow_json", flow),
                ):
                    if value is not None:
                        assignments.append(column + "=?")
                        values.append(_json(value))
                connection.execute(
                    "UPDATE creator_workspaces SET %s WHERE username=? AND project_id=?" %
                    ",".join(assignments),
                    tuple(values) + (username, project_id),
                )
                if fault_hook:
                    fault_hook("after_state")
                assistant = connection.execute(
                    """INSERT INTO creator_messages(
                       username,project_id,role,content,source_key,request_id,request_hash,
                       public_json,created_at) VALUES(?,?, 'assistant', ?, ?, NULL, '', ?, ?)""",
                    (username, project_id, str(reply or "")[:8000],
                     "profile-turn:%d" % int(user_message_id), _json(public or {}), now),
                )
                if fault_hook:
                    fault_hook("after_assistant")
                turn = {
                    "reply": str(reply or "")[:8000],
                    "message_public": public or {},
                    "assistant_message_id": int(assistant.lastrowid),
                }
                changed = connection.execute(
                    "UPDATE creator_messages SET public_json=? "
                    "WHERE id=? AND username=? AND project_id=? AND role='user'",
                    (_json({"turn": turn}), int(user_message_id), username, project_id),
                )
                if changed.rowcount != 1:
                    raise StateConflict("profile user request claim disappeared")
                if fault_hook:
                    fault_hook("before_commit")
                connection.commit()
                return turn
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _message(row):
        if not row:
            return None
        return {
            "id": row["id"], "role": row["role"], "content": row["content"],
            "public": _loads(row["public_json"], {}), "created_at": row["created_at"],
        }

    def add_message(self, username, project_id, role, content, *, source_key=None,
                    request_id=None, request_hash="", public=None, created_at=None):
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
                       (username,project_id,role,content,source_key,request_id,request_hash,
                        public_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (username, project_id, role, content, source_key, request_id,
                     str(request_hash or ""), _json(public or {}), now),
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
                if request_id and str(row["request_hash"] or "") != str(request_hash or ""):
                    raise IdempotencyConflict("request_id is bound to different input")
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

    def delete_message_if_unanswered(self, username, message_id):
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT role,public_json FROM creator_messages WHERE id=? AND username=?",
                (int(message_id), username),
            ).fetchone()
            public = _loads(row["public_json"], {}) if row else {}
            if (
                row and row["role"] == "user"
                and not (public or {}).get("response")
                and not (public or {}).get("turn")
            ):
                connection.execute(
                    "DELETE FROM creator_messages WHERE id=? AND username=?",
                    (int(message_id), username),
                )
                connection.commit()
                return True
            connection.rollback()
        return False

    @staticmethod
    def _job(row, include_private=False):
        if not row:
            return None
        value = {
            "id": row["id"], "batch_id": row["batch_id"],
            "platform": row["platform"], "version": row["version"],
            "revision": int(row["revision"]),
            "status": row["status"], "input": _loads(row["input_json"], {}),
            "quote": _loads(row["quote_json"], {}),
            "quote_expires_at": int(row["quote_expires_at"] or 0),
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
                "input_hash": row["input_hash"],
                "quote_cost": int(row["quote_cost"] or 0),
                "submit_input": _loads(row["submit_input_json"], {}),
                "submit_input_hash": row["submit_input_hash"],
                "submit_quote_token": row["submit_quote_token"],
                "submit_quote_cost": int(row["submit_quote_cost"] or 0),
                "submit_quote_expires_at": int(row["submit_quote_expires_at"] or 0),
                "submit_idempotency_key": row["submit_idempotency_key"],
            })
        return value

    def create_batch(self, username, project_id, topic, goal, platform_plans):
        if not platform_plans:
            raise StoreError("platform plans are required")
        now = int(time.time())
        batch_id = "creator_batch_" + uuid.uuid4().hex
        plan_hash = _digest(platform_plans)
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO creator_batches
                   (id,username,project_id,topic,goal,status,plan_json,plan_hash,
                    revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, username, project_id, topic, goal, "draft",
                 _json(platform_plans), plan_hash, 1, now, now),
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
                        input_hash,idempotency_key,revision,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("creator_job_" + uuid.uuid4().hex, batch_id, username, project_id,
                     platform, int(previous) + 1, "draft", _json(plan.get("input") or {}),
                     _digest(plan.get("input") or {}), "creator-agent-" + uuid.uuid4().hex,
                     1, now, now),
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
            "revision": int(row["revision"]),
            "plans": _loads(row["plan_json"], []), "quote": _loads(row["quote_json"], {}),
            "quote_expires_at": int(row["quote_expires_at"] or 0),
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
            else:
                value.update({
                    "plan_hash": row["plan_hash"],
                    "quoted_revision": int(row["quoted_revision"]),
                    "claim_id": row["claim_id"],
                })
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

    @staticmethod
    def _locked_batch(connection, username, batch_id):
        row = connection.execute(
            "SELECT * FROM creator_batches WHERE id=? AND username=?",
            (batch_id, username),
        ).fetchone()
        jobs = connection.execute(
            "SELECT * FROM creator_jobs WHERE batch_id=? AND username=? ORDER BY platform",
            (batch_id, username),
        ).fetchall() if row else []
        return row, jobs

    @staticmethod
    def _derived_batch_status(job_rows, current_status):
        statuses = [str(row["status"] or "") for row in job_rows]
        failed = {"error", "failed", "refunded", "failed_submission"}
        done = {"done", "completed", "success"}
        active = {
            "submit_claimed", "submission_unknown", "submitted", "queued",
            "running", "verifying", "processing",
        }
        if statuses and all(status in done for status in statuses):
            return "done"
        if statuses and all(status in failed for status in statuses):
            return "failed"
        if any(status in active for status in statuses):
            return "running"
        if any(status in done for status in statuses) and any(status in failed for status in statuses):
            return "partial"
        if current_status in {"draft", "ready", "quoting", "quoted"}:
            return current_status
        return "submitted"

    @staticmethod
    def _quote_valid(row, jobs, now, minimum_validity):
        threshold = int(now) + max(0, int(minimum_validity))
        return bool(
            row["status"] == "quoted"
            and int(row["quoted_revision"]) == int(row["revision"])
            and row["plan_hash"] == _digest(_loads(row["plan_json"], []))
            and int(row["quote_expires_at"] or 0) > threshold
            and jobs
            and all(
                job["status"] == "quoted"
                and bool(job["quote_token"])
                and int(job["quote_cost"] or 0) > 0
                and int(job["quote_expires_at"] or 0) > threshold
                and job["input_hash"] == _digest(_loads(job["input_json"], {}))
                for job in jobs
            )
        )

    @staticmethod
    def _clear_quote_locked(connection, username, batch_id, now):
        connection.execute(
            """UPDATE creator_jobs SET status='ready',quote_token='',quote_json='{}',
               quote_cost=0,quote_expires_at=0,confirmation_id='',job_id='',
               result_json='{}',error='',refund_status='',submit_input_json='{}',
               submit_input_hash='',submit_quote_token='',submit_quote_cost=0,
               submit_quote_expires_at=0,submit_idempotency_key='',
               revision=revision+1,updated_at=?
               WHERE batch_id=? AND username=? AND status='quoted'""",
            (now, batch_id, username),
        )
        connection.execute(
            """UPDATE creator_batches SET status='ready',quote_json='{}',
               quote_expires_at=0,confirmation_id='',quoted_revision=0,claim_id='',updated_at=?
               WHERE id=? AND username=? AND status='quoted'""",
            (now, batch_id, username),
        )

    def claim_quote(self, username, batch_id, expected_revision, now=None,
                    minimum_validity=0):
        claim_id = "quote_" + uuid.uuid4().hex
        now = int(time.time() if now is None else now)
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row:
                connection.rollback()
                raise StoreError("batch not found")
            if (
                row["status"] == "quoting" and row["claim_id"]
                and int(row["updated_at"] or 0) <= now - STALE_CLAIM_SECONDS
            ):
                connection.execute(
                    """UPDATE creator_batches SET status='ready',claim_id='',updated_at=?
                       WHERE id=? AND username=? AND status='quoting' AND claim_id=?""",
                    (now, batch_id, username, row["claim_id"]),
                )
                row, jobs = self._locked_batch(connection, username, batch_id)
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise StateConflict("batch revision changed")
            if row["status"] == "quoted":
                if self._quote_valid(row, jobs, now, minimum_validity):
                    connection.commit()
                    value = self.batch(username, batch_id, include_private=True)
                    value["quote_reused"] = True
                    return value
                self._clear_quote_locked(connection, username, batch_id, now)
                row, jobs = self._locked_batch(connection, username, batch_id)
            if row["status"] not in {"draft", "ready"} or row["claim_id"]:
                connection.rollback()
                raise StateConflict("batch is not quoteable")
            plans = _loads(row["plan_json"], [])
            if row["plan_hash"] != _digest(plans):
                connection.rollback()
                raise StateConflict("batch plan hash changed")
            if not jobs or any(
                row_job["input_hash"] != _digest(_loads(row_job["input_json"], {}))
                or row_job["status"] not in {"draft", "ready"}
                for row_job in jobs
            ):
                connection.rollback()
                raise StateConflict("batch jobs are not quoteable")
            changed = connection.execute(
                """UPDATE creator_batches SET status='quoting',claim_id=?,updated_at=?
                   WHERE id=? AND username=? AND revision=? AND status IN ('draft','ready')
                     AND claim_id=''""",
                (claim_id, now, batch_id, username, int(expected_revision)),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise StateConflict("batch quote claim lost")
            connection.commit()
        value = self.batch(username, batch_id, include_private=True)
        value["claim_id"] = claim_id
        value["quote_reused"] = False
        return value

    def finish_quote(self, username, batch_id, claim_id, job_quotes, quote, now=None):
        now = int(time.time() if now is None else now)
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row or row["status"] != "quoting" or row["claim_id"] != claim_id:
                connection.rollback()
                raise StateConflict("quote claim is stale")
            quotes = {str(item.get("id") or ""): item for item in job_quotes or []}
            if set(quotes) != {job["id"] for job in jobs}:
                connection.rollback()
                raise StateConflict("quote set is incomplete")
            expirations = []
            for job in jobs:
                item = quotes[job["id"]]
                try:
                    cost = int(item.get("cost") or 0)
                    expires_at = int(item.get("expires_at") or 0)
                except (TypeError, ValueError):
                    cost, expires_at = 0, 0
                if (
                    not item.get("quote_token")
                    or item.get("input_hash") != job["input_hash"]
                    or cost <= 0 or expires_at <= now
                ):
                    connection.rollback()
                    raise StateConflict("quote does not match current input")
                expirations.append(expires_at)
                changed = connection.execute(
                    """UPDATE creator_jobs SET status='quoted',quote_token=?,quote_json=?,
                       quote_cost=?,quote_expires_at=?,error='',revision=revision+1,updated_at=?
                       WHERE id=? AND username=? AND revision=? AND status IN ('draft','ready')
                          AND input_hash=?""",
                    (item["quote_token"], _json(item.get("quote") or {}), cost, expires_at, now,
                     job["id"], username, int(job["revision"]), job["input_hash"]),
                )
                if changed.rowcount != 1:
                    connection.rollback()
                    raise StateConflict("job quote finalize lost")
            earliest_expiry = min(expirations)
            frozen_quote = dict(quote or {})
            frozen_quote["expires_at"] = earliest_expiry
            frozen_quote["expires_in"] = max(0, earliest_expiry - now)
            changed = connection.execute(
                """UPDATE creator_batches SET status='quoted',quote_json=?,
                    quote_expires_at=?,quoted_revision=revision,claim_id='',updated_at=?
                    WHERE id=? AND username=? AND status='quoting' AND claim_id=?""",
                (_json(frozen_quote), earliest_expiry, now, batch_id, username, claim_id),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise StateConflict("batch quote finalize lost")
            connection.commit()
        return self.batch(username, batch_id)

    def abort_quote(self, username, batch_id, claim_id):
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE creator_batches SET status='ready',claim_id='',updated_at=?
                   WHERE id=? AND username=? AND status='quoting' AND claim_id=?""",
                (int(time.time()), batch_id, username, claim_id),
            )
            connection.commit()

    def claim_confirmation(self, username, batch_id, confirmation_id, expected_revision,
                           expected_quote_expires_at, now=None, safety_margin_seconds=0):
        now = int(time.time() if now is None else now)
        claimed_ids = []
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row:
                connection.rollback()
                raise StoreError("batch not found")
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise StateConflict("batch revision changed")
            if int(row["quote_expires_at"] or 0) != int(expected_quote_expires_at):
                connection.rollback()
                raise QuoteExpired("batch quote changed")
            existing_confirmation = str(row["confirmation_id"] or "")
            if existing_confirmation:
                if existing_confirmation != confirmation_id:
                    connection.rollback()
                    raise IdempotencyConflict("confirmation_id conflict")
                for job in jobs:
                    if job["status"] != "submission_unknown":
                        continue
                    changed = connection.execute(
                        """UPDATE creator_jobs SET status='submit_claimed',revision=revision+1,
                           updated_at=? WHERE id=? AND username=? AND revision=?
                           AND status='submission_unknown'""",
                        (now, job["id"], username, int(job["revision"])),
                    )
                    if changed.rowcount == 1:
                        claimed_ids.append(job["id"])
            else:
                if (
                    row["status"] != "quoted"
                    or int(row["quoted_revision"]) != int(row["revision"])
                    or row["plan_hash"] != _digest(_loads(row["plan_json"], []))
                    or not jobs
                ):
                    connection.rollback()
                    raise StateConflict("batch quote is stale")
                if any(
                    job["status"] != "quoted"
                    or not job["quote_token"]
                    or int(job["quote_cost"] or 0) <= 0
                    or job["input_hash"] != _digest(_loads(job["input_json"], {}))
                    for job in jobs
                ):
                    connection.rollback()
                    raise StateConflict("job quote is incomplete")
                if not self._quote_valid(row, jobs, now, safety_margin_seconds):
                    connection.rollback()
                    raise QuoteExpired("batch quote has insufficient validity")
                changed = connection.execute(
                    """UPDATE creator_batches SET status='submitting',confirmation_id=?,
                       claim_id='',updated_at=? WHERE id=? AND username=? AND revision=?
                       AND status='quoted' AND quoted_revision=revision""",
                    (confirmation_id, now, batch_id, username, int(expected_revision)),
                )
                if changed.rowcount != 1:
                    connection.rollback()
                    raise StateConflict("confirmation claim lost")
                for job in jobs:
                    changed = connection.execute(
                        """UPDATE creator_jobs SET status='submit_claimed',
                           confirmation_id=?,submit_input_json=input_json,
                           submit_input_hash=input_hash,
                           submit_quote_token=quote_token,
                           submit_quote_cost=quote_cost,
                           submit_quote_expires_at=quote_expires_at,
                           submit_idempotency_key=idempotency_key,
                           revision=revision+1,updated_at=?
                           WHERE id=? AND username=? AND revision=? AND status='quoted'
                             AND input_hash=?""",
                        (confirmation_id + ":" + job["platform"], now, job["id"], username,
                         int(job["revision"]), job["input_hash"]),
                    )
                    if changed.rowcount != 1:
                        connection.rollback()
                        raise StateConflict("job confirmation claim lost")
                    claimed_ids.append(job["id"])
            connection.commit()
        batch = self.batch(username, batch_id, include_private=True)
        batch["claimed_jobs"] = [
            job for job in batch["jobs"] if job["id"] in set(claimed_ids)
        ]
        return batch

    def claim_recovery(self, username, batch_id, now=None):
        now = int(time.time() if now is None else now)
        claimed_ids = []
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row or not row["confirmation_id"]:
                connection.rollback()
                return []
            for job in jobs:
                recoverable = job["status"] == "submission_unknown" or (
                    job["status"] == "submit_claimed"
                    and int(job["updated_at"] or 0) <= now - STALE_CLAIM_SECONDS
                )
                if not recoverable:
                    continue
                changed = connection.execute(
                    """UPDATE creator_jobs SET status='submit_claimed',revision=revision+1,
                       updated_at=? WHERE id=? AND username=? AND revision=?
                       AND status IN ('submission_unknown','submit_claimed')""",
                    (now, job["id"], username, int(job["revision"])),
                )
                if changed.rowcount == 1:
                    claimed_ids.append(job["id"])
            connection.commit()
        batch = self.batch(username, batch_id, include_private=True)
        return [job for job in batch["jobs"] if job["id"] in set(claimed_ids)]

    def finish_submit_claim(self, username, record_id, expected_revision, *, status,
                            job_id="", result=None, error="", refund_status=""):
        now = int(time.time())
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM creator_jobs WHERE id=? AND username=?",
                (record_id, username),
            ).fetchone()
            if not row or row["status"] != "submit_claimed" or int(row["revision"]) != int(expected_revision):
                connection.rollback()
                return False
            connection.execute(
                """UPDATE creator_jobs SET status=?,job_id=?,result_json=?,error=?,
                   refund_status=?,revision=revision+1,updated_at=?
                   WHERE id=? AND username=? AND revision=? AND status='submit_claimed'""",
                (status, str(job_id or ""), _json(result or {}), str(error or "")[:500],
                 str(refund_status or ""), now, record_id, username, int(expected_revision)),
            )
            batch_row, jobs = self._locked_batch(connection, username, row["batch_id"])
            derived = self._derived_batch_status(jobs, batch_row["status"])
            connection.execute(
                "UPDATE creator_batches SET status=?,updated_at=? WHERE id=? AND username=?",
                (derived, now, row["batch_id"], username),
            )
            connection.commit()
        return True

    def finish_task_poll(self, username, record_id, expected_revision, *, status,
                         result=None, error="", refund_status=""):
        now = int(time.time())
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM creator_jobs WHERE id=? AND username=?",
                (record_id, username),
            ).fetchone()
            allowed = {"submitted", "queued", "running", "verifying", "processing"}
            if not row or row["status"] not in allowed or int(row["revision"]) != int(expected_revision):
                connection.rollback()
                return False
            connection.execute(
                """UPDATE creator_jobs SET status=?,result_json=?,error=?,refund_status=?,
                   revision=revision+1,updated_at=? WHERE id=? AND username=?
                   AND revision=? AND status IN ('submitted','queued','running','verifying','processing')""",
                (status, _json(result or {}), str(error or "")[:500], str(refund_status or ""),
                 now, record_id, username, int(expected_revision)),
            )
            batch_row, jobs = self._locked_batch(connection, username, row["batch_id"])
            derived = self._derived_batch_status(jobs, batch_row["status"])
            connection.execute(
                "UPDATE creator_batches SET status=?,updated_at=? WHERE id=? AND username=?",
                (derived, now, row["batch_id"], username),
            )
            connection.commit()
        return True

    def recompute_batch(self, username, batch_id):
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row:
                connection.rollback()
                raise StoreError("batch not found")
            derived = self._derived_batch_status(jobs, row["status"])
            connection.execute(
                "UPDATE creator_batches SET status=?,updated_at=? WHERE id=? AND username=?",
                (derived, int(time.time()), batch_id, username),
            )
            connection.commit()
        return self.batch(username, batch_id)

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

    def replace_batch_plans(self, username, batch_id, platform_plans, expected_revision):
        now = int(time.time())
        incoming = {str(item.get("platform") or ""): item for item in platform_plans}
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, jobs = self._locked_batch(connection, username, batch_id)
            if not row:
                connection.rollback()
                raise StoreError("batch not found")
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise StateConflict("batch revision changed")
            if row["status"] not in {"draft", "ready", "quoted"} or row["claim_id"]:
                connection.rollback()
                raise StateConflict("batch is not editable")
            if set(incoming) != {job["platform"] for job in jobs}:
                connection.rollback()
                raise StateConflict("platform set cannot change during revision")
            next_revision = int(row["revision"]) + 1
            for job in jobs:
                tool_input = incoming[job["platform"]].get("input") or {}
                changed = connection.execute(
                    """UPDATE creator_jobs SET status='draft',input_json=?,input_hash=?,
                       quote_token='',quote_json='{}',quote_cost=0,quote_expires_at=0,
                       confirmation_id='',job_id='',
                       result_json='{}',error='',refund_status='',submit_input_json='{}',
                       submit_input_hash='',submit_quote_token='',submit_quote_cost=0,
                       submit_quote_expires_at=0,submit_idempotency_key='',revision=revision+1,
                       updated_at=? WHERE id=? AND username=? AND revision=?
                       AND status IN ('draft','ready','quoted')""",
                    (_json(tool_input), _digest(tool_input), now, job["id"], username,
                     int(job["revision"])),
                )
                if changed.rowcount != 1:
                    connection.rollback()
                    raise StateConflict("job edit claim lost")
            changed = connection.execute(
                """UPDATE creator_batches SET status='ready',plan_json=?,plan_hash=?,
                   quote_json='{}',quote_expires_at=0,confirmation_id='',quoted_revision=0,claim_id='',
                   revision=?,updated_at=? WHERE id=? AND username=? AND revision=?
                   AND status IN ('draft','ready','quoted') AND claim_id=''""",
                (_json(platform_plans), _digest(platform_plans), next_revision, now,
                 batch_id, username, int(expected_revision)),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise StateConflict("batch edit claim lost")
            connection.commit()
        return self.batch(username, batch_id, include_private=True)
