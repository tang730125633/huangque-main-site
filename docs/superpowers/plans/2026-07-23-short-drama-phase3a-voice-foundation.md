# Short Drama Phase 3-A Voice Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the persistent voice-line snapshot, authenticated read API, and read-only three-column voice workspace for short-drama projects in `voice_review`, without enabling paid generation or timeline mutation.

**Architecture:** Add an isolated `short_drama_voice.py` domain that owns the Phase 3 schema and converts the confirmed script/storyboard into an immutable source snapshot plus editable voice-stage copies. Extend the short-drama HTTP dispatcher with one authenticated GET endpoint, then route `voice_review` to a new pure frontend module while leaving the proven still-generation module unchanged.

**Tech Stack:** Python 3.12 standard library, SQLite, existing `content_domains` HTTP dispatcher, browser JavaScript using the existing UMD module pattern, CSS, Node.js 22 tests, Python `unittest`.

## Global Constraints

- This plan implements PR 3-A only; it does not submit TTS jobs, deduct points, edit timelines, lock shots, or advance to `video_review`.
- Preserve the existing `stills_review` behavior and all Phase 2 billing, idempotency, recovery, and refund paths.
- Use dedicated voice tables; do not widen the `short_drama_assets.type='still'` constraint.
- Store all timeline values as integer milliseconds, although PR 3-A leaves `start_ms` and `end_ms` unset.
- `source_text` is immutable after snapshot creation; initial `speech_text` and `subtitle_text` equal `source_text`.
- Treat `character_key='narrator'` as `line_type='narration'`; all other referenced lines are `dialogue`.
- Reuse `GET /api/gen/audio/voices`; do not add voice cloning or duplicate the audio voice catalog.
- Every HTTP read must enforce login, password-change status, canvas access, and project ownership without leaking another user's project.
- Render all project, character, line, and voice text through HTML escaping.
- Do not commit secrets, databases, generated media, test-server configuration, or temporary test directories.
- Keep the new frontend module read-only even for editors in PR 3-A.

---

## File Map

### Create

- `server/content_domains/short_drama_voice.py` — Phase 3 schema, voice-input normalization, snapshot initialization, and read snapshot.
- `site/workbench/canvas/canvas-short-drama-voice.js` — pure normalization/rendering plus read-only workspace lifecycle.
- `site/workbench/canvas/canvas-short-drama-voice.css` — isolated three-column voice workspace styles.
- `tests/test_short_drama_voice.py` — schema, snapshot, compatibility, authorization, and HTTP contract tests.
- `tests/test_canvas_short_drama_voice.js` — frontend state, escaping, voice catalog, lifecycle, and read-only tests.

### Modify

- `server/content_domains/short_drama.py:9,499-511,1351-1370,1450-1495` — import/init the voice domain and dispatch the authenticated GET route.
- `server/content_domains/short_drama_production.py:1207-1264` — initialize the voice snapshot inside the still-confirm transaction before advancing stage.
- `site/workbench/canvas/canvas-short-drama.js:15-20,568-642` — choose the voice module only for `voice_review`.
- `site/workbench/canvas.html:11-14,178-188` — load the new CSS and JS in dependency order.
- `scripts/stamp_assets.py:64-77` — register both new static assets.
- `.github/workflows/ci.yml:52-58` — run the new Node test in CI.
- `docs/api/openapi.json` — document the new read endpoint and response schema.
- `tests/test_canvas_short_drama.js:7-115,117-176,650-805` — protect OpenAPI, assets, cache stamps, and stage delegation.
- `tests/test_short_drama_production.py:760-870,1450-1520` — protect atomic still-to-voice handoff.

---

### Task 1: Add the Phase 3 voice schema

**Files:**

- Create: `server/content_domains/short_drama_voice.py`
- Create: `tests/test_short_drama_voice.py`
- Modify: `server/content_domains/short_drama.py:499-511`

**Interfaces:**

- Consumes: `db_factory() -> sqlite3.Connection` used throughout `short_drama.py`.
- Produces: `short_drama_voice.init_db(db_factory) -> None`.
- Produces tables: `short_drama_voice_shots`, `short_drama_voice_lines`, `short_drama_voice_versions`, `short_drama_voice_jobs`, `short_drama_voice_quotes`, and `short_drama_voice_charge_attempts`.

- [ ] **Step 1: Write the failing schema tests**

Create `tests/test_short_drama_voice.py` with imports, a temporary database, and exact schema assertions:

```python
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_voice


class ShortDramaVoiceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_all_voice_tables_and_is_idempotent(self):
        short_drama.init_db(self.db)
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({
            "short_drama_voice_shots",
            "short_drama_voice_lines",
            "short_drama_voice_versions",
            "short_drama_voice_jobs",
            "short_drama_voice_quotes",
            "short_drama_voice_charge_attempts",
        }.issubset(tables))

    def test_voice_line_and_job_constraints_reject_cross_project_links(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_projects "
                "(id,username,title,synopsis,ratio,target_duration,shot_count,"
                "visual_style,target_platform,stage,revision,created_at,updated_at) "
                "VALUES ('p1','alice','A','long enough','9:16',30,6,'film','douyin',"
                "'voice_review',1,1,1)"
            )
            conn.execute(
                "INSERT INTO short_drama_projects "
                "(id,username,title,synopsis,ratio,target_duration,shot_count,"
                "visual_style,target_platform,stage,revision,created_at,updated_at) "
                "VALUES ('p2','alice','B','long enough','9:16',30,6,'film','douyin',"
                "'voice_review',1,1,1)"
            )
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,"
                "scene_description,camera_description,character_keys_json,"
                "dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES ('s1','p1',1,'shot-1',0,5,'scene','camera','[]','[]','image','video')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_shots "
                    "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                    "VALUES ('s1','p2',0,1,1,1)"
                )
```

- [ ] **Step 2: Run the schema test and verify red**

Run:

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
```

Expected: import or table assertions fail because `short_drama_voice.py` and its tables do not exist.

- [ ] **Step 3: Create the complete schema and initializer**

Create `server/content_domains/short_drama_voice.py` with standard-library imports and `_SCHEMA` containing the six tables. Use these exact public names:

```python
"""Voice-line snapshots and read models for short-drama production."""

import hashlib
import json
import sqlite3
import time
import uuid


VOICE_STAGES = {
    "voice_review", "video_review", "assembly_review", "completed",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_voice_shots (
  shot_id TEXT PRIMARY KEY REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  timeline_revision INTEGER NOT NULL DEFAULT 1 CHECK (timeline_revision >= 1),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_voice_lines (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  dialogue_line_id TEXT,
  line_type TEXT NOT NULL CHECK (line_type IN ('dialogue','narration')),
  sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
  character_key TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL,
  speech_text TEXT NOT NULL,
  subtitle_text TEXT NOT NULL,
  subtitle_visible INTEGER NOT NULL DEFAULT 1 CHECK (subtitle_visible IN (0,1)),
  voice_key TEXT NOT NULL DEFAULT '',
  speed REAL NOT NULL DEFAULT 1.0 CHECK (speed >= 0.5 AND speed <= 2.0),
  pitch INTEGER NOT NULL DEFAULT 0 CHECK (pitch >= -12 AND pitch <= 12),
  volume INTEGER NOT NULL DEFAULT 0 CHECK (volume >= -50 AND volume <= 100),
  current_version INTEGER,
  start_ms INTEGER CHECK (start_ms IS NULL OR start_ms >= 0),
  end_ms INTEGER CHECK (end_ms IS NULL OR end_ms > 0),
  input_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id, sort_order)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_versions (
  id TEXT PRIMARY KEY,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id INTEGER NOT NULL UNIQUE,
  audio_file TEXT NOT NULL DEFAULT '',
  audio_url TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms > 0),
  speech_text TEXT NOT NULL,
  voice_key TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('metadata_pending','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(voice_line_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  quoted_cost INTEGER NOT NULL CHECK (quoted_cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending','running','metadata_pending','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0,1,2)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_quotes (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_idempotency_key TEXT,
  consumed_job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_voice_charge_attempts (
  charge_key TEXT PRIMARY KEY,
  refund_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  quote_token TEXT NOT NULL REFERENCES short_drama_voice_quotes(token),
  cost INTEGER NOT NULL CHECK (cost >= 0),
  audio_payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('accepted','charged','linked','done','refund_pending','refunded','failed')
  ),
  points_left INTEGER,
  job_id INTEGER,
  terminal_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, endpoint, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_lines_project
  ON short_drama_voice_lines(project_id, shot_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_jobs_project
  ON short_drama_voice_jobs(username, project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_quotes_lookup
  ON short_drama_voice_quotes(username, project_id, voice_line_id, expires_at);
CREATE TRIGGER IF NOT EXISTS short_drama_voice_shots_project_guard
BEFORE INSERT ON short_drama_voice_shots
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_lines_project_guard
BEFORE INSERT ON short_drama_voice_lines
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice line shot must belong to project');
END;
"""


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
```

Import the new module in `short_drama.py` and initialize it after the base project tables and still-production tables:

```python
from . import short_drama_production, short_drama_voice
```

```python
    short_drama_production.init_db(db_factory)
    short_drama_voice.init_db(db_factory)
```

- [ ] **Step 4: Run schema tests and existing project tests**

Run:

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests tests.test_short_drama_projects -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the schema slice**

```powershell
git add server/content_domains/short_drama_voice.py server/content_domains/short_drama.py tests/test_short_drama_voice.py
git commit -m "feat: add short drama voice schema"
```

---

### Task 2: Build the immutable voice snapshot and read model

**Files:**

- Modify: `server/content_domains/short_drama_voice.py`
- Modify: `tests/test_short_drama_voice.py`

**Interfaces:**

- Consumes: confirmed rows from `short_drama_projects`, `short_drama_characters`, `short_drama_scripts`, and `short_drama_shots`.
- Produces: `voice_input_hash(speech_text, voice_key, speed, pitch, volume) -> str`.
- Produces: `ensure_voice_workspace(conn, project_id, allowed_stages=None) -> None`; caller owns transaction and commit.
- Produces: `get_voice_workspace(db_factory, username, project_id) -> dict`.

- [ ] **Step 1: Add failing snapshot tests with a concrete project**

Add a helper that creates two characters (`detective`, `narrator`), two linked lines in the first shot, and five silent shots. Add tests for mapping, idempotency, immutability, and historical lazy initialization:

```python
def voice_plan():
    dialogue = [
        {"id": "line-1", "character_key": "detective", "text": "谁在那里？"},
        {"id": "line-2", "character_key": "narrator", "text": "门外没有回答。"},
    ]
    characters = [
        {
            "character_key": "detective", "name": "林探长",
            "identity_text": "detective", "personality": "calm",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "coat", "wardrobe_prompt": "dark coat",
            "voice_key": "longwan",
            "voice_settings": {"speed": 1.2, "pitch": 1, "volume": 4},
            "sort_order": 0,
        },
        {
            "character_key": "narrator", "name": "旁白",
            "identity_text": "narrator", "personality": "steady",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "voice only", "wardrobe_prompt": "none",
            "voice_key": "longcheng", "voice_settings": {},
            "sort_order": 1,
        },
    ]
    shots = []
    for index in range(6):
        shots.append({
            "shot_key": "shot-%d" % (index + 1), "sort_order": index,
            "duration": 5, "scene_description": "scene",
            "camera_description": "camera",
            "character_keys": ["detective", "narrator"] if index == 0 else [],
            "dialogue_line_ids": ["line-1", "line-2"] if index == 0 else [],
            "image_prompt": "image", "video_prompt": "video",
        })
    return {
        "characters": characters,
        "script": {
            "title": "Night", "logline": "visitor", "hook": "knock",
            "conflict_text": "silence", "turn_text": "empty",
            "ending": "door opens", "dialogue_lines": dialogue,
        },
        "shots": shots,
    }
```

```python
class ShortDramaVoiceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        payload = {
            "title": "Night", "synopsis": "A detective hears a midnight knock.",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
        }
        project = short_drama.create_project(self.db, "alice", payload)
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            voice_plan(), planning_cost=0, planning_job_id=501,
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_lazy_snapshot_maps_dialogue_narration_defaults_and_silent_shots(self):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("voice_review", snapshot["stage"])
        self.assertEqual(6, len(snapshot["shots"]))
        first = snapshot["shots"][0]
        self.assertEqual(["dialogue", "narration"], [
            line["line_type"] for line in first["lines"]
        ])
        self.assertEqual(["谁在那里？", "门外没有回答。"], [
            line["source_text"] for line in first["lines"]
        ])
        self.assertEqual("longwan", first["lines"][0]["voice_key"])
        self.assertEqual(1.2, first["lines"][0]["speed"])
        self.assertEqual("pending", first["status"])
        self.assertTrue(all(shot["status"] == "silent" for shot in snapshot["shots"][1:]))

    def test_snapshot_is_idempotent_and_does_not_resync_source_changes(self):
        first = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        line_id = first["shots"][0]["lines"][0]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_lines SET speech_text='custom' WHERE id=?",
                (line_id,),
            )
            script = conn.execute(
                "SELECT id,dialogue_lines_json FROM short_drama_scripts "
                "WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (self.project["id"],),
            ).fetchone()
            lines = json.loads(script[1])
            lines[0]["text"] = "changed upstream"
            conn.execute(
                "UPDATE short_drama_scripts SET dialogue_lines_json=? WHERE id=?",
                (json.dumps(lines, ensure_ascii=False), script[0]),
            )
            conn.commit()
        second = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(line_id, second["shots"][0]["lines"][0]["id"])
        self.assertEqual("谁在那里？", second["shots"][0]["lines"][0]["source_text"])
        self.assertEqual("custom", second["shots"][0]["lines"][0]["speech_text"])
```

- [ ] **Step 2: Run the snapshot tests and verify red**

Run:

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSnapshotTests -v
```

Expected: fail because the snapshot functions are absent.

- [ ] **Step 3: Implement normalization, hash, initialization, and snapshot**

Add these private helpers and public functions to `short_drama_voice.py`:

```python
def _json_value(raw, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value


def _number(value, default, minimum, maximum, integer=False):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    result = max(minimum, min(maximum, result))
    return int(round(result)) if integer else round(result, 1)


def normalized_voice_settings(raw):
    value = raw if isinstance(raw, dict) else {}
    return {
        "speed": _number(value.get("speed"), 1.0, 0.5, 2.0),
        "pitch": _number(value.get("pitch"), 0, -12, 12, integer=True),
        "volume": _number(value.get("volume"), 0, -50, 100, integer=True),
    }


def voice_input_hash(speech_text, voice_key, speed, pitch, volume):
    descriptor = {
        "speech_text": str(speech_text),
        "voice_key": str(voice_key),
        "speed": float(speed),
        "pitch": int(pitch),
        "volume": int(volume),
    }
    encoded = json.dumps(
        descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_voice_workspace(conn, project_id, allowed_stages=None):
    conn.row_factory = sqlite3.Row
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND deleted=0",
        (project_id,),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    allowed = set(allowed_stages or VOICE_STAGES)
    if project["stage"] not in allowed:
        raise ValueError("短剧项目尚未进入配音阶段")
    existing = conn.execute(
        "SELECT 1 FROM short_drama_voice_shots WHERE project_id=? LIMIT 1",
        (project_id,),
    ).fetchone()
    if existing:
        return
    script = conn.execute(
        "SELECT dialogue_lines_json FROM short_drama_scripts "
        "WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if not script:
        raise ValueError("短剧项目缺少已确认剧本")
    dialogue_items = _json_value(script["dialogue_lines_json"], [])
    dialogue = {
        item.get("id"): item for item in dialogue_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    characters = {
        row["character_key"]: row for row in conn.execute(
            "SELECT * FROM short_drama_characters WHERE project_id=?",
            (project_id,),
        )
    }
    shots = conn.execute(
        "SELECT * FROM short_drama_shots WHERE project_id=? "
        "ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    if not shots:
        raise ValueError("短剧项目缺少已确认分镜")
    now = int(time.time())
    for shot in shots:
        conn.execute(
            "INSERT INTO short_drama_voice_shots "
            "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
            "VALUES (?,?,0,1,?,?)",
            (shot["id"], project_id, now, now),
        )
        line_ids = _json_value(shot["dialogue_line_ids_json"], [])
        for sort_order, dialogue_line_id in enumerate(line_ids):
            source = dialogue.get(dialogue_line_id)
            if not source:
                raise ValueError("分镜引用了不存在的台词")
            character_key = str(source.get("character_key") or "")
            character = characters.get(character_key)
            if not character:
                raise ValueError("台词引用了不存在的角色")
            settings = normalized_voice_settings(
                _json_value(character["voice_settings_json"], {})
            )
            speech_text = str(source.get("text") or "").strip()
            if not speech_text:
                raise ValueError("配音台词不能为空")
            voice_key = str(character["voice_key"] or "").strip()
            input_hash = voice_input_hash(
                speech_text, voice_key, settings["speed"],
                settings["pitch"], settings["volume"],
            )
            conn.execute(
                "INSERT INTO short_drama_voice_lines "
                "(id,project_id,shot_id,dialogue_line_id,line_type,sort_order,"
                "character_key,source_text,speech_text,subtitle_text,"
                "subtitle_visible,voice_key,speed,pitch,volume,current_version,"
                "start_ms,end_ms,input_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,NULL,NULL,NULL,?,?,?)",
                (
                    str(uuid.uuid4()), project_id, shot["id"], dialogue_line_id,
                    "narration" if character_key == "narrator" else "dialogue",
                    sort_order, character_key, speech_text, speech_text, speech_text,
                    voice_key, settings["speed"], settings["pitch"],
                    settings["volume"], input_hash, now, now,
                ),
            )


def _line_snapshot(row, character_name):
    return {
        "id": row["id"],
        "dialogue_line_id": row["dialogue_line_id"],
        "line_type": row["line_type"],
        "sort_order": row["sort_order"],
        "character_key": row["character_key"],
        "character_name": character_name,
        "source_text": row["source_text"],
        "speech_text": row["speech_text"],
        "subtitle_text": row["subtitle_text"],
        "subtitle_visible": bool(row["subtitle_visible"]),
        "voice_key": row["voice_key"],
        "speed": row["speed"],
        "pitch": row["pitch"],
        "volume": row["volume"],
        "current_version": row["current_version"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "input_hash": row["input_hash"],
        "versions": [],
        "job": None,
    }


def build_voice_snapshot(conn, project):
    conn.row_factory = sqlite3.Row
    characters = {
        row["character_key"]: row["name"] for row in conn.execute(
            "SELECT character_key,name FROM short_drama_characters WHERE project_id=?",
            (project["id"],),
        )
    }
    voice_shots = {
        row["shot_id"]: row for row in conn.execute(
            "SELECT * FROM short_drama_voice_shots WHERE project_id=?",
            (project["id"],),
        )
    }
    lines = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_voice_lines WHERE project_id=? "
        "ORDER BY shot_id,sort_order",
        (project["id"],),
    ):
        lines.setdefault(row["shot_id"], []).append(
            _line_snapshot(row, characters.get(row["character_key"], row["character_key"]))
        )
    shots = []
    for shot in conn.execute(
        "SELECT id,shot_key,sort_order,duration FROM short_drama_shots "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        shot_lines = lines.get(shot["id"], [])
        state = voice_shots[shot["id"]]
        shots.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "locked": bool(state["locked"]),
            "timeline_revision": state["timeline_revision"],
            "status": "silent" if not shot_lines else "pending",
            "lines": shot_lines,
        })
    return {
        "project_id": project["id"],
        "revision": project["revision"],
        "stage": project["stage"],
        "ratio": project["ratio"],
        "target_duration": project["target_duration"],
        "point_budget": project["point_budget"],
        "spent_points": project["spent_points"],
        "reserved_points": 0,
        "shots": shots,
    }


def get_voice_workspace(db_factory, username, project_id):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        ensure_voice_workspace(conn, project_id)
        snapshot = build_voice_snapshot(conn, project)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 4: Run snapshot and schema tests**

Run:

```powershell
python -m unittest tests.test_short_drama_voice -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit snapshot behavior**

```powershell
git add server/content_domains/short_drama_voice.py tests/test_short_drama_voice.py
git commit -m "feat: snapshot short drama voice lines"
```

---

### Task 3: Make still-to-voice handoff atomic and expose the read API

**Files:**

- Modify: `server/content_domains/short_drama_production.py:1207-1264`
- Modify: `server/content_domains/short_drama.py:1351-1495`
- Modify: `tests/test_short_drama_production.py`
- Modify: `tests/test_short_drama_voice.py`
- Modify: `docs/api/openapi.json`

**Interfaces:**

- Consumes: `short_drama_voice.ensure_voice_workspace(conn, project_id, allowed_stages)`.
- Consumes: existing `_project_username_for_access(..., write=False)` for owner/editor/viewer resolution.
- Produces: authenticated `GET /api/gen/short-drama/voice?project_id=<id>`.
- Produces response: the exact dictionary returned by `get_voice_workspace`.

- [ ] **Step 1: Add failing atomic handoff and HTTP tests**

Add to `tests/test_short_drama_production.py`:

```python
def test_confirm_stills_creates_voice_snapshot_in_the_same_transaction(self):
    self._lock_every_current_still()
    confirmed = short_drama_production.confirm_stage(
        self.db, "alice", {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
            "stage": "stills_review",
        },
    )
    self.assertEqual("voice_review", confirmed["stage"])
    with closing(self.db()) as conn:
        shot_count = conn.execute(
            "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
            (self.project["id"],),
        ).fetchone()[0]
    self.assertEqual(6, shot_count)


def test_snapshot_failure_rolls_back_stage_confirmation(self):
    self._lock_every_current_still()
    with mock.patch(
        "content_domains.short_drama_voice.ensure_voice_workspace",
        side_effect=RuntimeError("snapshot failed"),
    ):
        with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
            short_drama_production.confirm_stage(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "revision": self.project["revision"],
                    "stage": "stills_review",
                },
            )
    with closing(self.db()) as conn:
        stage = conn.execute(
            "SELECT stage FROM short_drama_projects WHERE id=?",
            (self.project["id"],),
        ).fetchone()[0]
    self.assertEqual("stills_review", stage)
```

Add to `tests/test_short_drama_voice.py` a minimal handler and route test:

```python
class GetHandler:
    def __init__(self, path, token="alice"):
        self.path = path
        self.token = token
        self.response = None

    def _token(self):
        return self.token

    def _send(self, status, payload):
        self.response = (status, payload)


def test_voice_get_route_requires_auth_and_returns_owned_snapshot(self):
    handler = GetHandler(
        "/api/gen/short-drama/voice?project_id=" + self.project["id"]
    )
    handled = short_drama.dispatch_http(
        handler, "GET", self.db,
        lambda token: {"username": token, "must_change": False} if token else None,
    )
    self.assertTrue(handled)
    self.assertEqual(200, handler.response[0])
    self.assertEqual(self.project["id"], handler.response[1]["project_id"])

    anonymous = GetHandler(handler.path, token="")
    short_drama.dispatch_http(anonymous, "GET", self.db, lambda _token: None)
    self.assertEqual(401, anonymous.response[0])

    other = GetHandler(handler.path, token="mallory")
    short_drama.dispatch_http(
        other, "GET", self.db,
        lambda token: {"username": token, "must_change": False},
    )
    self.assertEqual(404, other.response[0])

    with closing(self.db()) as conn:
        conn.execute(
            "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
            (self.project["id"],),
        )
        conn.commit()
    viewer = GetHandler(handler.path, token="viewer")
    short_drama.dispatch_http(
        viewer, "GET", self.db,
        lambda token: {"username": token, "must_change": False},
        canvas_access_resolver=lambda _handler: {
            "board_id": "board-a", "role": "viewer",
        },
    )
    self.assertEqual(200, viewer.response[0])
```

- [ ] **Step 2: Run handoff and route tests and verify red**

Run:

```powershell
python -m unittest tests.test_short_drama_voice tests.test_short_drama_production.ShortDramaProductionTests.test_confirm_stills_creates_voice_snapshot_in_the_same_transaction tests.test_short_drama_production.ShortDramaProductionTests.test_snapshot_failure_rolls_back_stage_confirmation -v
```

Expected: failures because stage confirmation does not initialize voice data and the route is absent.

- [ ] **Step 3: Initialize the snapshot inside the existing still transaction**

Import `short_drama_voice` in `short_drama_production.py`:

```python
from . import short_drama_voice
```

Immediately after validating all locked stills and before the compare-and-swap stage update, call:

```python
        short_drama_voice.ensure_voice_workspace(
            conn, project_id, allowed_stages={"stills_review"}
        )
```

Do not commit inside `ensure_voice_workspace`; the existing `confirm_stage` transaction owns both snapshot creation and stage advancement.

- [ ] **Step 4: Add the authenticated GET route**

Add `"/api/gen/short-drama/voice"` to `_HTTP_ROUTES["GET"]`.

In `dispatch_http`, place the voice branch before the generic `elif method == "GET"` branch:

```python
        elif method == "GET" and path.endswith("/voice"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_voice.get_voice_workspace(
                    db_factory, owner, project_id
                ),
            )
```

Document `GET /api/gen/short-drama/voice` in `docs/api/openapi.json` with:

- required query parameter `project_id: string`;
- `200` response object containing required `project_id`, `revision`, `stage`, `ratio`, `target_duration`, `point_budget`, `spent_points`, `reserved_points`, and `shots`;
- `401`, `403`, `404`, and `400` responses using the repository's existing error schema;
- description stating the endpoint is read-only and free.

Add this path object under `paths`:

```json
"/api/gen/short-drama/voice": {
  "get": {
    "tags": ["短剧"],
    "summary": "读取短剧配音字幕工作区",
    "description": "只读且不扣点；首次读取历史 voice_review 项目时幂等建立配音快照。",
    "security": [{"bearerAuth": []}],
    "parameters": [{
      "name": "project_id",
      "in": "query",
      "required": true,
      "schema": {"type": "string", "minLength": 1}
    }],
    "responses": {
      "200": {
        "description": "配音字幕工作区快照",
        "content": {"application/json": {"schema": {
          "type": "object",
          "required": [
            "project_id", "revision", "stage", "ratio", "target_duration",
            "point_budget", "spent_points", "reserved_points", "shots"
          ],
          "properties": {
            "project_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "stage": {"type": "string", "enum": ["voice_review", "video_review", "assembly_review", "completed"]},
            "ratio": {"type": "string", "enum": ["9:16", "16:9"]},
            "target_duration": {"type": "integer", "enum": [30, 45, 60]},
            "point_budget": {"type": "integer", "minimum": 0},
            "spent_points": {"type": "integer", "minimum": 0},
            "reserved_points": {"type": "integer", "minimum": 0},
            "shots": {
              "type": "array",
              "items": {
                "type": "object",
                "required": ["id", "shot_key", "sort_order", "duration", "locked", "status", "lines"],
                "properties": {
                  "id": {"type": "string"},
                  "shot_key": {"type": "string"},
                  "sort_order": {"type": "integer"},
                  "duration": {"type": "integer", "enum": [5, 10]},
                  "locked": {"type": "boolean"},
                  "status": {"type": "string", "enum": ["silent", "pending"]},
                  "lines": {"type": "array", "items": {"type": "object"}}
                }
              }
            }
          }
        }}}
      },
      "400": {"description": "项目尚未进入配音阶段", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
      "401": {"description": "未登录", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
      "403": {"description": "必须修改初始密码或无画布读取权限", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
      "404": {"description": "项目不存在或当前用户无权读取", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    }
  }
}
```

- [ ] **Step 5: Run backend and OpenAPI regressions**

Run:

```powershell
python -m unittest tests.test_short_drama_voice tests.test_short_drama_production tests.test_short_drama_projects -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the handoff and API**

```powershell
git add server/content_domains/short_drama.py server/content_domains/short_drama_production.py server/content_domains/short_drama_voice.py tests/test_short_drama_voice.py tests/test_short_drama_production.py docs/api/openapi.json
git commit -m "feat: expose short drama voice workspace"
```

---

### Task 4: Build the pure read-only voice workspace

**Files:**

- Create: `site/workbench/canvas/canvas-short-drama-voice.js`
- Create: `site/workbench/canvas/canvas-short-drama-voice.css`
- Create: `tests/test_canvas_short_drama_voice.js`

**Interfaces:**

- Consumes: `client.json(path, options?) -> Promise<object>`.
- Consumes: `GET /api/gen/short-drama/voice?project_id=...`.
- Consumes: `GET /api/gen/audio/voices`.
- Produces UMD export: `HQCanvas.shortDramaVoice`.
- Produces module API: `normalizeState(input, voices, options)`, `renderWorkspace(input, options)`, and `createWorkspace(options)`.
- Produces workspace API: `ready`, `render()`, `reload()`, `selectShot(shotId)`, `destroy()`, `getState()`.

- [ ] **Step 1: Write failing frontend contract tests**

Create `tests/test_canvas_short_drama_voice.js`:

```javascript
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const voice = require('../site/workbench/canvas/canvas-short-drama-voice.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1', revision: 8, stage: 'voice_review',
    ratio: '9:16', target_duration: 30,
    point_budget: 100, spent_points: 12, reserved_points: 0,
    shots: [
      {
        id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 5,
        locked: false, timeline_revision: 1, status: 'pending',
        lines: [{
          id: 'voice-1', dialogue_line_id: 'line-1', line_type: 'dialogue',
          sort_order: 0, character_key: 'detective',
          character_name: '林<script>探长', source_text: '谁在那里？',
          speech_text: '谁在那里？', subtitle_text: '<b>谁在那里？</b>',
          subtitle_visible: true, voice_key: 'longwan',
          speed: 1.2, pitch: 1, volume: 4,
          current_version: null, start_ms: null, end_ms: null,
          versions: [], job: null,
        }],
      },
      {
        id: 'shot-2', shot_key: '第二镜', sort_order: 1, duration: 5,
        locked: false, timeline_revision: 1, status: 'silent', lines: [],
      },
    ],
  }, overrides);
}

const voices = [
  { voice_key: 'longwan', display_name: '龙婉', preview_url: '/voice.mp3' },
  { voice_key: 'longcheng', display_name: '龙城', preview_url: '' },
];

async function testNormalizeAndRender() {
  assert.deepEqual(
    Object.keys(voice).sort(),
    ['createWorkspace', 'normalizeState', 'renderWorkspace']
  );
  const normalized = voice.normalizeState(snapshot(), voices, {});
  assert.equal(normalized.selectedShotId, 'shot-1');
  assert.equal(normalized.shots[0].lines[0].voice_name, '龙婉');
  const html = voice.renderWorkspace(snapshot(), { voices });
  assert.match(html, /镜头列表[\s\S]*台词与字幕[\s\S]*配音控制台/);
  assert.match(html, /第一镜[\s\S]*第二镜/);
  assert.match(html, /龙婉/);
  assert.match(html, /谁在那里？/);
  assert.doesNotMatch(html, /<script>|<b>/);
  assert.match(html, /林&lt;script&gt;探长/);
  assert.match(html, /&lt;b&gt;谁在那里？&lt;\/b&gt;/);
  assert.match(html, /data-action="generate-line"[^>]*disabled/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
}

async function testWorkspaceLoadsBothResourcesAndDestroysCleanly() {
  const calls = [];
  const client = {
    json(route) {
      calls.push(route);
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(snapshot());
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const workspace = voice.createWorkspace({
    projectId: 'project-1', client, document: null,
  });
  await workspace.ready;
  assert.deepEqual(calls, [
    '/api/gen/short-drama/voice?project_id=project-1',
    '/api/gen/audio/voices',
  ]);
  assert.match(workspace.render(), /龙婉/);
  assert.equal(workspace.selectShot('shot-2'), true);
  assert.match(workspace.render(), /当前镜头没有台词/);
  assert.equal(workspace.selectShot('missing'), false);
  workspace.destroy();
  assert.equal(await workspace.reload(), null);
}

async function main() {
  await testNormalizeAndRender();
  await testWorkspaceLoadsBothResourcesAndDestroysCleanly();
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-voice.css'
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  console.log('canvas short drama voice: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
```

- [ ] **Step 2: Run the frontend test and verify red**

Run:

```powershell
node tests/test_canvas_short_drama_voice.js
```

Expected: fail because the JS and CSS files do not exist.

- [ ] **Step 3: Implement the UMD module with read-only controls**

Create `canvas-short-drama-voice.js` using the same UMD shape as the still module. Implement:

```javascript
(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDramaVoice=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var VOICE_PATH='/api/gen/short-drama/voice';
  var VOICES_PATH='/api/gen/audio/voices';

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);
    return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=clone(value[key]); });
      return copy;
    }
    return value;
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function voiceItems(input){
    var items=Array.isArray(input)?input:(input&&Array.isArray(input.items)?input.items:[]);
    return items.map(function(item){
      return {
        voice_key:text(item.voice_key),
        display_name:text(item.display_name||item.voice_key||'未命名音色'),
        preview_url:text(item.preview_url)
      };
    });
  }
  function normalizeLine(line,index,voiceMap){
    line=line&&typeof line==='object'?line:{};
    var voiceKey=text(line.voice_key);
    return {
      id:line.id,sort_order:number(line.sort_order,index),
      line_type:line.line_type==='narration'?'narration':'dialogue',
      character_key:text(line.character_key),
      character_name:text(line.character_name||line.character_key),
      source_text:text(line.source_text),speech_text:text(line.speech_text),
      subtitle_text:text(line.subtitle_text),
      subtitle_visible:line.subtitle_visible!==false,
      voice_key:voiceKey,
      voice_name:voiceMap[voiceKey]?voiceMap[voiceKey].display_name:(voiceKey||'未选择音色'),
      speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0),
      current_version:line.current_version,start_ms:line.start_ms,end_ms:line.end_ms
    };
  }
  function normalizeState(input,voices,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var catalog=voiceItems(voices),voiceMap={};
    catalog.forEach(function(item){ voiceMap[item.voice_key]=item; });
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot&&typeof shot==='object'?shot:{};
      return {
        id:shot.id,shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),duration:number(shot.duration,0),
        locked:!!shot.locked,status:text(shot.status||'pending'),
        lines:(Array.isArray(shot.lines)?shot.lines:[]).map(function(line,lineIndex){
          return normalizeLine(line,lineIndex,voiceMap);
        })
      };
    }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var selected=options.selectedShotId||input.selectedShotId;
    if(!shots.some(function(shot){ return shot.id===selected; })) selected=shots[0]&&shots[0].id;
    return {
      project_id:input.project_id,revision:number(input.revision,0),
      stage:text(input.stage||'voice_review'),ratio:text(input.ratio||'9:16'),
      point_budget:number(input.point_budget,0),spent_points:number(input.spent_points,0),
      reserved_points:number(input.reserved_points,0),shots:shots,voices:catalog,
      selectedShotId:selected,busy:!!options.busy,error:text(options.error),
      destroyed:!!options.destroyed
    };
  }
  function selectedShot(state){
    return state.shots.find(function(shot){ return shot.id===state.selectedShotId; })||null;
  }
  function renderWorkspace(input,options){
    options=options||{};
    var state=normalizeState(input,options.voices,options);
    var shot=selectedShot(state);
    var rail=state.shots.map(function(item){
      return '<button type="button" class="nc-sdv-shot'+
        (item.id===state.selectedShotId?' is-selected':'')+
        '" data-shot-id="'+escapeHtml(item.id)+'"><strong>'+escapeHtml(item.shot_key)+
        '</strong><small>'+item.duration+' 秒 · '+item.lines.length+' 句 · '+
        escapeHtml(item.status)+'</small></button>';
    }).join('');
    var lines=shot?shot.lines.map(function(line){
      return '<article class="nc-sdv-line"><header><strong>'+
        escapeHtml(line.character_name)+'</strong><span>'+escapeHtml(line.voice_name)+
        '</span></header><label>发音文本<textarea disabled>'+
        escapeHtml(line.speech_text)+'</textarea></label><label>字幕文本<textarea disabled>'+
        escapeHtml(line.subtitle_text)+'</textarea></label><div class="nc-sdv-params">'+
        '<span>语速 '+line.speed+'</span><span>音调 '+line.pitch+
        '</span><span>音量 '+line.volume+'</span></div>'+
        '<button type="button" data-action="generate-line" disabled>生成配音</button></article>';
    }).join(''):'';
    return '<div class="nc-short-drama-voice" data-busy="'+state.busy+'">'+
      '<aside class="nc-sdv-rail"><header><span>配音字幕</span><h2>镜头列表</h2></header>'+
      rail+'</aside><main class="nc-sdv-editor"><header><span>逐句资产</span>'+
      '<h2>台词与字幕</h2></header>'+(shot&&shot.lines.length?lines:
      '<section class="nc-sdv-empty">当前镜头没有台词，将作为静音镜头。</section>')+
      '<section class="nc-sdv-timeline">字幕时间轴将在配音生成后显示。</section></main>'+
      '<aside class="nc-sdv-inspector"><header><span>只读基础阶段</span>'+
      '<h2>配音控制台</h2></header><dl><div><dt>项目预算</dt><dd>'+
      state.point_budget+' 点</dd></div><div><dt>累计已用</dt><dd>'+
      state.spent_points+' 点</dd></div></dl>'+
      '<button type="button" data-action="generate-shot" disabled>生成当前镜头</button>'+
      '<button type="button" data-action="save-timeline" disabled>保存字幕时间轴</button>'+
      '<p>本批次仅开放数据核对和音色映射，付费生成将在下一批次验收。</p>'+
      (state.error?'<div role="alert">'+escapeHtml(state.error)+'</div>':'')+
      '</aside></div>';
  }
  function createWorkspace(options){
    options=options||{};
    if(!options.client||typeof options.client.json!=='function') throw new Error('voice workspace requires a JSON client');
    if(!options.projectId) throw new Error('voice workspace requires projectId');
    var destroyed=false,snapshot=null,voices=[],host=options.host||null;
    var ui={busy:true,error:'',selectedShotId:options.selectedShotId};
    function render(){
      var html=renderWorkspace(snapshot||{},{
        voices:voices,busy:ui.busy,error:ui.error,
        selectedShotId:ui.selectedShotId,destroyed:destroyed
      });
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function selectShot(shotId){
      if(destroyed||!snapshot||!Array.isArray(snapshot.shots)) return false;
      var exists=snapshot.shots.some(function(shot){ return shot.id===shotId; });
      if(!exists) return false;
      ui.selectedShotId=shotId;render();return true;
    }
    function onClick(event){
      var node=event&&event.target;
      while(node&&node!==host){
        if(node.getAttribute&&node.getAttribute('data-shot-id')!=null){
          selectShot(node.getAttribute('data-shot-id'));return;
        }
        node=node.parentNode;
      }
    }
    if(host&&typeof host.addEventListener==='function') host.addEventListener('click',onClick);
    function reload(){
      if(destroyed) return Promise.resolve(null);
      ui.busy=true;ui.error='';render();
      return Promise.all([
        options.client.json(VOICE_PATH+'?project_id='+encodeURIComponent(options.projectId)),
        options.client.json(VOICES_PATH)
      ]).then(function(results){
        if(destroyed) return null;
        snapshot=results[0];voices=voiceItems(results[1]);ui.busy=false;render();
        return snapshot;
      }).catch(function(error){
        if(destroyed) return null;
        ui.busy=false;ui.error=text(error&&error.message||error);render();
        return null;
      });
    }
    var ready=reload();
    return {
      projectId:options.projectId,ready:ready,render:render,reload:reload,
      selectShot:selectShot,
      getState:function(){ return clone(normalizeState(snapshot||{},voices,ui)); },
      destroy:function(){
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('click',onClick);
        destroyed=true;host=null;snapshot=null;voices=[];
      }
    };
  }
  return {
    normalizeState:normalizeState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
```

- [ ] **Step 4: Add the isolated CSS**

Create `canvas-short-drama-voice.css` with a three-column layout, scrolling regions, disabled control treatment, and compact breakpoint:

```css
.nc-short-drama-voice {
  --sdv-ink: #172033;
  --sdv-muted: #667085;
  --sdv-line: #e5e7eb;
  --sdv-paper: #fff;
  --sdv-canvas: #f4f3ef;
  --sdv-accent: #8b5cf6;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 300px;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  color: var(--sdv-ink);
  background: var(--sdv-canvas);
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.nc-short-drama-voice *,
.nc-short-drama-voice *::before,
.nc-short-drama-voice *::after { box-sizing: border-box; }
.nc-sdv-rail,
.nc-sdv-editor,
.nc-sdv-inspector { min-width: 0; overflow: auto; padding: 20px; }
.nc-sdv-rail { border-right: 1px solid var(--sdv-line); background: var(--sdv-paper); }
.nc-sdv-inspector { border-left: 1px solid var(--sdv-line); background: var(--sdv-paper); }
.nc-sdv-rail header,
.nc-sdv-editor > header,
.nc-sdv-inspector header { margin-bottom: 16px; }
.nc-sdv-rail h2,
.nc-sdv-editor h2,
.nc-sdv-inspector h2 { margin: 4px 0 0; }
.nc-sdv-shot,
.nc-sdv-inspector button,
.nc-sdv-line button {
  width: 100%;
  margin: 0 0 8px;
  padding: 10px 12px;
  border: 1px solid var(--sdv-line);
  border-radius: 10px;
  color: inherit;
  background: var(--sdv-paper);
  text-align: left;
}
.nc-sdv-shot { display: grid; gap: 4px; cursor: default; }
.nc-sdv-shot small { color: var(--sdv-muted); }
.nc-sdv-shot.is-selected {
  border-color: var(--sdv-accent);
  background: color-mix(in srgb, var(--sdv-accent) 8%, var(--sdv-paper));
}
.nc-sdv-line,
.nc-sdv-empty,
.nc-sdv-timeline {
  margin-bottom: 14px;
  padding: 16px;
  border: 1px solid var(--sdv-line);
  border-radius: 12px;
  background: var(--sdv-paper);
}
.nc-sdv-line header { display: flex; justify-content: space-between; gap: 12px; }
.nc-sdv-line label { display: grid; gap: 6px; margin-top: 12px; }
.nc-sdv-line textarea {
  min-height: 72px;
  resize: vertical;
  border: 1px solid var(--sdv-line);
  border-radius: 8px;
  padding: 9px;
  color: inherit;
  background: #fafafa;
}
.nc-sdv-params { display: flex; gap: 12px; margin: 10px 0; color: var(--sdv-muted); }
.nc-sdv-inspector dl div { display: flex; justify-content: space-between; }
.nc-sdv-inspector button:disabled,
.nc-sdv-line button:disabled { cursor: not-allowed; opacity: .48; }
@media (max-width: 980px) {
  .nc-short-drama-voice {
    grid-template-columns: 220px minmax(0, 1fr);
    overflow: auto;
  }
  .nc-sdv-inspector { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--sdv-line); }
}
@media (max-width: 700px) {
  .nc-short-drama-voice { display: block; overflow: auto; }
  .nc-sdv-rail,
  .nc-sdv-editor,
  .nc-sdv-inspector { overflow: visible; border: 0; border-bottom: 1px solid var(--sdv-line); }
}
```

- [ ] **Step 5: Run frontend tests**

Run:

```powershell
node tests/test_canvas_short_drama_voice.js
node --check site/workbench/canvas/canvas-short-drama-voice.js
```

Expected: both commands exit successfully and the test prints `canvas short drama voice: pass`.

- [ ] **Step 6: Commit the pure workspace**

```powershell
git add site/workbench/canvas/canvas-short-drama-voice.js site/workbench/canvas/canvas-short-drama-voice.css tests/test_canvas_short_drama_voice.js
git commit -m "feat: add read-only short drama voice workspace"
```

---

### Task 5: Route `voice_review` to the new module and wire static assets

**Files:**

- Modify: `site/workbench/canvas/canvas-short-drama.js:568-642`
- Modify: `site/workbench/canvas.html:11-14,178-188`
- Modify: `scripts/stamp_assets.py:64-77`
- Modify: `.github/workflows/ci.yml:52-58`
- Modify: `tests/test_canvas_short_drama.js`

**Interfaces:**

- Consumes: `HQCanvas.shortDramaProduction` for `stills_review` and existing later-stage fallback.
- Consumes: `HQCanvas.shortDramaVoice` for `voice_review`.
- Produces injection option: `voiceModule`, parallel to existing `productionModule`.

- [ ] **Step 1: Add failing stage-routing and asset tests**

Update `testCanvasIntegration()` in `tests/test_canvas_short_drama.js`:

```javascript
const voiceSource = fs.readFileSync(
  path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-voice.js'),
  'utf8'
).replace(/\r\n/g, '\n');
const voiceCss = fs.readFileSync(
  path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-voice.css'),
  'utf8'
).replace(/\r\n/g, '\n');

assert.ok(html.includes('canvas/canvas-short-drama-voice.css?v='));
assert.ok(html.includes('canvas/canvas-short-drama-voice.js?v='));
assert.ok(
  html.indexOf('canvas/canvas-short-drama-voice.js?v=') <
  html.indexOf('canvas/canvas-short-drama.js?v=')
);
assert.ok(ci.includes('node tests/test_canvas_short_drama_voice.js'));
```

Add both new assets to the existing cache-stamp loop.

In the delegated workspace test, create separate counters:

```javascript
const stillCalls = [];
const voiceCalls = [];
const stillModule = {
  createWorkspace(options) {
    stillCalls.push(options.projectId);
    return {
      ready: Promise.resolve(),
      render() { return '<section>still workspace</section>'; },
      reload() { return Promise.resolve(); },
      destroy() {},
    };
  },
};
const voiceModule = {
  createWorkspace(options) {
    voiceCalls.push(options.projectId);
    return {
      ready: Promise.resolve(),
      render() { return '<section>voice workspace</section>'; },
      reload() { return Promise.resolve(); },
      destroy() {},
    };
  },
};
```

Assert:

```javascript
assert.deepEqual(stillCalls, ['project-stills']);
assert.deepEqual(voiceCalls, ['project-voice']);
```

Create workspaces with `productionModule: stillModule` and `voiceModule: voiceModule`; ensure `stills_review` selects only the former and `voice_review` selects only the latter.

- [ ] **Step 2: Run the routing test and verify red**

Run:

```powershell
node tests/test_canvas_short_drama.js
```

Expected: fail because the new assets are not loaded and `voice_review` still selects the still module.

- [ ] **Step 3: Select the module by stage**

Inside `activateProductionWorkspace`, replace the single module lookup with:

```javascript
      var isVoiceStage=project&&project.stage==='voice_review';
      var moduleOption=isVoiceStage?'voiceModule':'productionModule';
      var defaultModule=isVoiceStage?
        (root&&root.HQCanvas&&root.HQCanvas.shortDramaVoice):
        (root&&root.HQCanvas&&root.HQCanvas.shortDramaProduction);
      var productionModule=Object.prototype.hasOwnProperty.call(options,moduleOption)?
        options[moduleOption]:defaultModule;
```

Do not change the still confirmation adapter or the existing `productionModule` injection contract. The voice module does not call `confirm` in PR 3-A.

- [ ] **Step 4: Load, stamp, and test the static assets**

In `canvas.html`, add:

```html
<link rel="stylesheet" href="canvas/canvas-short-drama-voice.css?v=00000000">
```

after the still production CSS, and:

```html
<script src="canvas/canvas-short-drama-voice.js?v=00000000"></script>
```

after the still production JS but before `canvas-short-drama.js`.

Add to `scripts/stamp_assets.py`:

```python
    Asset("canvas/canvas-short-drama-voice.js", required=False),
    Asset("canvas/canvas-short-drama-voice.css", required=False),
```

Add to the short-drama CI step:

```yaml
          node tests/test_canvas_short_drama_voice.js
```

Run the stamper to replace the temporary hash values:

```powershell
python scripts/stamp_assets.py
```

- [ ] **Step 5: Run frontend and static-asset regressions**

Run:

```powershell
node tests/test_canvas_short_drama_voice.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
```

Expected: every command exits successfully.

- [ ] **Step 6: Commit stage routing and assets**

```powershell
git add site/workbench/canvas/canvas-short-drama.js site/workbench/canvas.html scripts/stamp_assets.py .github/workflows/ci.yml tests/test_canvas_short_drama.js
git commit -m "feat: route short drama voice review workspace"
```

---

### Task 6: Final PR 3-A verification and review handoff

**Files:**

- Verify only; change files only if a verification failure identifies a PR 3-A defect.

**Interfaces:**

- Confirms all interfaces produced by Tasks 1-5.

- [ ] **Step 1: Run the complete targeted Python suite**

Run:

```powershell
python -m unittest tests.test_short_drama_voice tests.test_short_drama_projects tests.test_short_drama_planning tests.test_short_drama_production -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run the complete targeted JavaScript suite**

Run:

```powershell
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama_voice.js
```

Expected: each test prints its pass marker and exits successfully.

- [ ] **Step 3: Run syntax, CI, and stamp checks**

Run:

```powershell
python -m py_compile server/content_domains/short_drama.py server/content_domains/short_drama_production.py server/content_domains/short_drama_voice.py
node --check site/workbench/canvas/canvas-short-drama.js
node --check site/workbench/canvas/canvas-short-drama-production.js
node --check site/workbench/canvas/canvas-short-drama-voice.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check origin/main...HEAD
```

Expected: all commands exit successfully and `git diff --check` prints no errors.

- [ ] **Step 4: Perform a local read-only acceptance check**

Start the local content API and static site:

```powershell
bash scripts/dev_local.sh
```

Then open `http://127.0.0.1:8097/workbench/canvas.html`, select a project at `voice_review`, and verify:

1. The page loads the voice workspace instead of the still workspace.
2. Six shots appear in storyboard order.
3. Linked dialogue text, character names, default voice keys, and voice settings match the confirmed plan.
4. A `narrator` line is labelled as narration.
5. Silent shots show the silent state.
6. All generation, save, lock, and stage-confirm controls are absent or disabled.
7. Refresh returns the same voice-line IDs and does not overwrite snapshot text.
8. A viewer can read the workspace but cannot gain write controls.
9. Another user cannot discover or read the project.

Record the tested project ID and outcome in the PR description; do not commit the database or generated files.

- [ ] **Step 5: Confirm the commit and worktree scope**

Run:

```powershell
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: the worktree is clean; commits contain only PRD/plan plus PR 3-A files; there are no secrets, databases, generated outputs, or unrelated edits.

- [ ] **Step 6: Request code review before publishing the PR**

Use `superpowers:requesting-code-review` to review the completed implementation against:

- `docs/superpowers/specs/2026-07-23-short-drama-phase3-voice-subtitles-design.md`;
- this PR 3-A plan;
- the nine local acceptance checks above.

Address only verified PR 3-A findings, rerun the affected checks, then use the branch-finishing workflow to push and open a draft PR for user acceptance.

---

## Plan Self-Review

- Spec coverage: Tasks 1-3 cover the dedicated schema, immutable line snapshot, historical lazy initialization, atomic still handoff, authenticated read API, owner/editor/viewer access, and OpenAPI contract.
- Frontend coverage: Tasks 4-5 cover voice catalog reuse, escaped read-only rendering, shot selection, workspace lifecycle, stage-specific delegation, static assets, cache stamps, and CI.
- PR boundary: paid generation, audio duration probing, version mutation, subtitle editing, playback, locking, and `voice_review -> video_review` remain outside PR 3-A and are assigned to PR 3-B through PR 3-E in the approved PRD.
- Type consistency: `ensure_voice_workspace`, `get_voice_workspace`, `normalizeState`, `renderWorkspace`, `createWorkspace`, and `voiceModule` use the same names and argument shapes in producers, consumers, and tests.
- Content scan: the plan contains no unresolved implementation markers or ambiguous file targets.
