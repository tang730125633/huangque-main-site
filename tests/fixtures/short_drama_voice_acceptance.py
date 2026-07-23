"""Build synthetic, disposable data for PR #19 browser acceptance."""

import argparse
import json
import secrets
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = str(REPOSITORY_ROOT / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import auth_server
from content_domains import short_drama, short_drama_voice


def _outside_repository(path):
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return resolved
    raise ValueError("acceptance databases must live outside the repository")


def _plan():
    characters = [
        {
            "character_key": "host", "name": "林主持", "identity_text": "host",
            "personality": "calm", "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "studio host", "wardrobe_prompt": "dark jacket",
            "voice_key": "longwan",
            "voice_settings": {"speed": 1.2, "pitch": 1, "volume": 4},
            "sort_order": 0,
        },
        {
            "character_key": "narrator", "name": "旁白", "identity_text": "narrator",
            "personality": "steady", "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "voice only", "wardrobe_prompt": "none",
            "voice_key": "longcheng", "voice_settings": {}, "sort_order": 1,
        },
    ]
    dialogue = []
    shots = []
    for index in range(6):
        line_ids = []
        character_keys = []
        if index < 5:
            line_id = "acceptance-line-%d" % (index + 1)
            character_key = "narrator" if index == 1 else "host"
            dialogue.append({
                "id": line_id, "character_key": character_key,
                "text": "验收台词 %d" % (index + 1),
            })
            line_ids = [line_id]
            character_keys = [character_key]
        shots.append({
            "shot_key": "acceptance-shot-%d" % (index + 1),
            "sort_order": index, "duration": 5,
            "scene_description": "验收场景 %d" % (index + 1),
            "camera_description": "固定镜头", "character_keys": character_keys,
            "dialogue_line_ids": line_ids, "image_prompt": "synthetic image",
            "video_prompt": "synthetic video",
        })
    return {
        "characters": characters,
        "script": {
            "title": "PR19 验收短剧", "logline": "隔离验收", "hook": "开始",
            "conflict_text": "验证", "turn_text": "刷新", "ending": "完成",
            "dialogue_lines": dialogue,
        },
        "shots": shots,
    }


def _create_auth_fixture(auth_db, board_id, users, passwords):
    previous_db = auth_server.DB
    auth_server.DB = str(auth_db)
    try:
        auth_server.init_db()
        with closing(sqlite3.connect(auth_db)) as conn:
            for username in users:
                salt = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,"
                    "must_change,account_id) VALUES (?,?,?,?,1000,'member',0,?)",
                    (username, auth_server.hash_pw(passwords[username], salt), salt,
                     username, uuid.uuid4().hex),
                )
            conn.execute(
                "INSERT INTO canvas_boards(id,owner_username,name,data_json,version,"
                "created_at,updated_at) VALUES (?,?,?,'{}',1,1,1)",
                (board_id, users[0], "PR19 浏览器验收"),
            )
            conn.executemany(
                "INSERT INTO canvas_members(board_id,username,role,invited_by,created_at) "
                "VALUES (?,?,?,?,1)",
                [(board_id, users[0], "editor", users[0]),
                 (board_id, users[1], "viewer", users[0])],
            )
            conn.commit()
    finally:
        auth_server.DB = previous_db


def build_acceptance_fixture(content_db, auth_db):
    content_db = _outside_repository(content_db)
    auth_db = _outside_repository(auth_db)
    if content_db == auth_db:
        raise ValueError("content and auth databases must be different")
    content_db.parent.mkdir(parents=True, exist_ok=True)
    auth_db.parent.mkdir(parents=True, exist_ok=True)

    suffix = uuid.uuid4().hex[:8]
    owner = "pr19-owner-" + suffix
    viewer = "pr19-viewer-" + suffix
    unauthorized = "pr19-outsider-" + suffix
    board_id = "pr19-board-" + suffix
    passwords = {name: secrets.token_urlsafe(18) for name in (owner, viewer, unauthorized)}
    _create_auth_fixture(auth_db, board_id, (owner, viewer, unauthorized), passwords)

    db_factory = lambda: sqlite3.connect(content_db)
    short_drama.init_db(db_factory)
    with closing(db_factory()) as conn:
        conn.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, kind TEXT, username TEXT, cost INTEGER,
            status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0,
            refunded INTEGER DEFAULT 0, owner TEXT
        )""")
        conn.commit()
    project = short_drama.create_project(db_factory, owner, {
        "title": "PR19 浏览器验收", "synopsis": "仅使用合成数据的配音工作区验收。",
        "ratio": "9:16", "target_duration": 30, "shot_count": 6,
    })
    project = short_drama.apply_plan(
        db_factory, owner, project["id"], project["revision"], _plan(),
        planning_cost=0, planning_job_id=910019,
    )
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE short_drama_projects SET board_id=?,stage='voice_review' WHERE id=?",
            (board_id, project["id"]),
        )
        short_drama_voice.ensure_voice_workspace(
            conn, project["id"], allowed_stages={"voice_review"},
        )
        project_row = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=?", (project["id"],),
        ).fetchone()
        snapshot = short_drama_voice.build_voice_snapshot(conn, project_row)
        conn.commit()
    voice_line_ids = [
        line["id"] for shot in snapshot["shots"] for line in shot["lines"]
    ]
    return {
        "project_id": project["id"], "board_id": board_id,
        "owner": owner, "viewer": viewer, "unauthorized": unauthorized,
        "passwords": passwords, "voice_line_ids": voice_line_ids,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-db", required=True)
    parser.add_argument("--auth-db", required=True)
    parser.add_argument("--runtime-json", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    fixture = build_acceptance_fixture(args.content_db, args.auth_db)
    runtime_path = _outside_repository(args.runtime_json)
    runtime_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    evidence_path = Path(args.evidence).resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        "# PR #19 浏览器验收\n\n"
        "- 项目 ID：%s\n- 画布 ID：%s\n- Owner：%s\n- Viewer：%s\n"
        "- Unauthorized：%s\n\n## 检查结果\n" % (
            fixture["project_id"], fixture["board_id"], fixture["owner"],
            fixture["viewer"], fixture["unauthorized"],
        ),
        encoding="utf-8",
    )
    print("acceptance fixture ready: %s / %s" % (
        fixture["project_id"], fixture["board_id"],
    ))


if __name__ == "__main__":
    main()
