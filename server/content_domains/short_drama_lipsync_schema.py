"""Idempotent SQLite schema for the short-drama lipsync domain."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_lipsync_visual_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('image','video')),
  uri TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  locked_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_lipsync_visual_current
  ON short_drama_lipsync_visual_sources(project_id,shot_id,source_kind)
  WHERE is_current=1;

CREATE TABLE IF NOT EXISTS short_drama_lipsync_media_reports (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES short_drama_lipsync_visual_sources(id)
    ON DELETE CASCADE,
  probe_version TEXT NOT NULL,
  width INTEGER NOT NULL CHECK (width > 0),
  height INTEGER NOT NULL CHECK (height > 0),
  fps REAL NOT NULL CHECK (fps > 0),
  duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
  codec TEXT NOT NULL,
  source_format TEXT NOT NULL,
  report_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(source_id,report_hash)
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_quotes (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  business_key TEXT NOT NULL,
  quote_revision INTEGER NOT NULL CHECK (quote_revision >= 1),
  pricing_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_capability_version TEXT NOT NULL,
  profile TEXT NOT NULL,
  face_target_json TEXT NOT NULL,
  provider_contract_json TEXT NOT NULL DEFAULT '{}',
  duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
  media_spec_json TEXT NOT NULL,
  cost_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('issued','expired','invalidated','consumed')
  ),
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(business_key,pricing_version,quote_revision)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_lipsync_quotes_lookup
  ON short_drama_lipsync_quotes(
    actor_username,business_key,pricing_version,status,expires_at
  );

CREATE TABLE IF NOT EXISTS short_drama_lipsync_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  quote_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_quotes(id),
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  charge_key TEXT NOT NULL UNIQUE,
  refund_key TEXT NOT NULL UNIQUE,
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  state TEXT NOT NULL CHECK (
    state IN (
      'accepted','charged','linked','settled','failed',
      'refund_pending','refunded','manual_review'
    )
  ),
  charge_ref TEXT,
  refund_ref TEXT,
  points_left INTEGER,
  terminal_json TEXT NOT NULL DEFAULT '{}',
  recovery_token TEXT,
  recovery_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username,idempotency_key)
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_jobs (
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_attempts(id),
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  input_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_job_id TEXT,
  provider_create_started_at INTEGER,
  state TEXT NOT NULL CHECK (
    state IN (
      'prepared','queued','running','succeeded','failed',
      'cancel_pending','cancelled','manual_review'
    )
  ),
  lease_token TEXT,
  lease_owner TEXT,
  lease_expires_at INTEGER,
  heartbeat_at INTEGER,
  next_poll_at INTEGER,
  poll_count INTEGER NOT NULL DEFAULT 0 CHECK (poll_count >= 0),
  result_retry_count INTEGER NOT NULL DEFAULT 0
    CHECK (result_retry_count >= 0),
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  result_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_lipsync_provider_job
  ON short_drama_lipsync_jobs(provider,provider_job_id)
  WHERE provider_job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS short_drama_lipsync_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_jobs(id),
  input_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_version TEXT NOT NULL,
  dependency_hashes_json TEXT NOT NULL,
  media_spec_json TEXT NOT NULL,
  file TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  cost_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id,shot_id,version)
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_current (
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  version_id TEXT NOT NULL REFERENCES short_drama_lipsync_versions(id),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  locked_at INTEGER,
  locked_by TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id,shot_id)
);

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_source_project_guard
BEFORE INSERT ON short_drama_lipsync_visual_sources
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT,'lipsync visual shot must belong to project');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_quote_project_guard
BEFORE INSERT ON short_drama_lipsync_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT,'lipsync quote shot must belong to project');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_quote_immutable
BEFORE UPDATE OF
  actor_username,owner_username,project_id,shot_id,business_key,
  quote_revision,pricing_version,input_hash,provider,
  provider_capability_version,profile,face_target_json,duration_ms,
  media_spec_json,cost_json,expires_at,created_at
ON short_drama_lipsync_quotes
BEGIN
  SELECT RAISE(ABORT,'lipsync quote identity and price are immutable');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_version_project_guard
BEFORE INSERT ON short_drama_lipsync_versions
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT,'lipsync version shot must belong to project');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_version_immutable
BEFORE UPDATE ON short_drama_lipsync_versions
BEGIN
  SELECT RAISE(ABORT,'lipsync versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_current_guard_insert
BEFORE INSERT ON short_drama_lipsync_current
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_lipsync_versions
  WHERE id=NEW.version_id
    AND project_id=NEW.project_id AND shot_id=NEW.shot_id
)
BEGIN
  SELECT RAISE(ABORT,'lipsync current version must belong to shot');
END;

CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_current_guard_update
BEFORE UPDATE OF version_id,project_id,shot_id ON short_drama_lipsync_current
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_lipsync_versions
  WHERE id=NEW.version_id
    AND project_id=NEW.project_id AND shot_id=NEW.shot_id
)
BEGIN
  SELECT RAISE(ABORT,'lipsync current version must belong to shot');
END;
"""


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        _migrate_pr_e_tables(conn)
        conn.executescript("BEGIN;\n" + SCHEMA + "\nCOMMIT;")
        if "provider_contract_json" not in _columns(
            conn, "short_drama_lipsync_quotes"
        ):
            conn.execute(
                "ALTER TABLE short_drama_lipsync_quotes "
                "ADD COLUMN provider_contract_json TEXT NOT NULL DEFAULT '{}'"
            )
            conn.commit()
        conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS short_drama_lipsync_quote_contract_immutable
        BEFORE UPDATE OF provider_contract_json ON short_drama_lipsync_quotes
        BEGIN
          SELECT RAISE(ABORT,'lipsync provider contract is immutable');
        END;
        """)
        if "provider_create_started_at" not in _columns(
            conn, "short_drama_lipsync_jobs"
        ):
            conn.execute(
                "ALTER TABLE short_drama_lipsync_jobs "
                "ADD COLUMN provider_create_started_at INTEGER"
            )
            conn.commit()
        if "result_retry_count" not in _columns(
            conn, "short_drama_lipsync_jobs"
        ):
            conn.execute(
                "ALTER TABLE short_drama_lipsync_jobs "
                "ADD COLUMN result_retry_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK (result_retry_count >= 0)"
            )
            conn.commit()
        current_columns = _columns(conn, "short_drama_lipsync_current")
        if "locked_at" not in current_columns:
            conn.execute(
                "ALTER TABLE short_drama_lipsync_current "
                "ADD COLUMN locked_at INTEGER"
            )
            conn.commit()
        if "locked_by" not in current_columns:
            conn.execute(
                "ALTER TABLE short_drama_lipsync_current "
                "ADD COLUMN locked_by TEXT"
            )
            conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn, table):
    return {
        str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)
    }


def _migrate_pr_e_tables(conn):
    """Upgrade the simulation-only PR-E placeholders without losing history."""
    columns = _columns(conn, "short_drama_lipsync_attempts")
    if not columns or "charge_key" in columns:
        return
    conn.executescript("""
    BEGIN IMMEDIATE;
    DROP TRIGGER IF EXISTS short_drama_lipsync_version_immutable;
    DROP TRIGGER IF EXISTS short_drama_lipsync_version_project_guard;
    DROP TRIGGER IF EXISTS short_drama_lipsync_current_guard_insert;
    DROP TRIGGER IF EXISTS short_drama_lipsync_current_guard_update;

    ALTER TABLE short_drama_lipsync_current
      RENAME TO short_drama_lipsync_current_pr_e;
    ALTER TABLE short_drama_lipsync_versions
      RENAME TO short_drama_lipsync_versions_pr_e;
    ALTER TABLE short_drama_lipsync_jobs
      RENAME TO short_drama_lipsync_jobs_pr_e;
    ALTER TABLE short_drama_lipsync_attempts
      RENAME TO short_drama_lipsync_attempts_pr_e;

    CREATE TABLE short_drama_lipsync_attempts (
      id TEXT PRIMARY KEY,
      actor_username TEXT NOT NULL,
      owner_username TEXT NOT NULL,
      quote_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_quotes(id),
      idempotency_key TEXT NOT NULL,
      request_hash TEXT NOT NULL,
      charge_key TEXT NOT NULL UNIQUE,
      refund_key TEXT NOT NULL UNIQUE,
      cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
      state TEXT NOT NULL CHECK (
        state IN (
          'accepted','charged','linked','settled','failed',
          'refund_pending','refunded','manual_review'
        )
      ),
      charge_ref TEXT,
      refund_ref TEXT,
      points_left INTEGER,
      terminal_json TEXT NOT NULL DEFAULT '{}',
      recovery_token TEXT,
      recovery_at INTEGER,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(actor_username,idempotency_key)
    );
    INSERT INTO short_drama_lipsync_attempts
      (id,actor_username,owner_username,quote_id,idempotency_key,request_hash,
       charge_key,refund_key,cost,state,charge_ref,refund_ref,created_at,updated_at)
    SELECT attempt.id,attempt.actor_username,quote.owner_username,attempt.quote_id,
      attempt.idempotency_key,attempt.request_hash,
      'short-drama-lipsync:' || attempt.actor_username || ':' ||
        attempt.idempotency_key,
      'short-drama-lipsync:' || attempt.actor_username || ':' ||
        attempt.idempotency_key || ':refund',
      COALESCE(CAST(json_extract(quote.cost_json,'$.points') AS INTEGER),0),
      CASE attempt.state
        WHEN 'prepared' THEN 'accepted'
        WHEN 'job_created' THEN 'linked'
        ELSE attempt.state
      END,
      attempt.charge_ref,attempt.refund_ref,attempt.created_at,attempt.updated_at
    FROM short_drama_lipsync_attempts_pr_e attempt
    JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id;

    CREATE TABLE short_drama_lipsync_jobs (
      id TEXT PRIMARY KEY,
      attempt_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_attempts(id),
      project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
      shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
      input_hash TEXT NOT NULL,
      provider TEXT NOT NULL,
      provider_job_id TEXT,
      provider_create_started_at INTEGER,
      state TEXT NOT NULL CHECK (
        state IN (
          'prepared','queued','running','succeeded','failed',
          'cancel_pending','cancelled','manual_review'
        )
      ),
      lease_token TEXT,
      lease_owner TEXT,
      lease_expires_at INTEGER,
      heartbeat_at INTEGER,
      next_poll_at INTEGER,
      poll_count INTEGER NOT NULL DEFAULT 0 CHECK (poll_count >= 0),
      result_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (result_retry_count >= 0),
      progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
      result_json TEXT NOT NULL DEFAULT '{}',
      error_json TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    INSERT INTO short_drama_lipsync_jobs
      (id,attempt_id,project_id,shot_id,input_hash,provider,provider_job_id,state,
       heartbeat_at,error_json,created_at,updated_at)
    SELECT job.id,job.attempt_id,quote.project_id,quote.shot_id,quote.input_hash,
      job.provider,job.provider_job_id,job.state,job.heartbeat_at,
      job.error_json,job.created_at,job.updated_at
    FROM short_drama_lipsync_jobs_pr_e job
    JOIN short_drama_lipsync_attempts_pr_e attempt ON attempt.id=job.attempt_id
    JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id;

    CREATE TABLE short_drama_lipsync_versions (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
      shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
      version INTEGER NOT NULL CHECK (version >= 1),
      job_id TEXT NOT NULL UNIQUE REFERENCES short_drama_lipsync_jobs(id),
      input_hash TEXT NOT NULL,
      provider TEXT NOT NULL,
      model_version TEXT NOT NULL,
      dependency_hashes_json TEXT NOT NULL,
      media_spec_json TEXT NOT NULL,
      file TEXT NOT NULL,
      file_hash TEXT NOT NULL,
      cost_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      UNIQUE(project_id,shot_id,version)
    );
    INSERT INTO short_drama_lipsync_versions SELECT *
      FROM short_drama_lipsync_versions_pr_e;

    CREATE TABLE short_drama_lipsync_current (
      project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
      shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
      version_id TEXT NOT NULL REFERENCES short_drama_lipsync_versions(id),
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
      updated_at INTEGER NOT NULL,
      PRIMARY KEY(project_id,shot_id)
    );
    INSERT INTO short_drama_lipsync_current SELECT *
      FROM short_drama_lipsync_current_pr_e;

    DROP TABLE short_drama_lipsync_current_pr_e;
    DROP TABLE short_drama_lipsync_versions_pr_e;
    DROP TABLE short_drama_lipsync_jobs_pr_e;
    DROP TABLE short_drama_lipsync_attempts_pr_e;
    COMMIT;
    """)
