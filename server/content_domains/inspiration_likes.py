import json
import sqlite3
import time
from contextlib import closing


def _connect(db_path):
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def clean_inspiration_id(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("灵感案例 ID 必须是正整数")
    try:
        inspiration_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("灵感案例 ID 必须是正整数")
    if inspiration_id <= 0:
        raise ValueError("灵感案例 ID 必须是正整数")
    return str(inspiration_id)


def summary(db_path, username=None):
    with closing(_connect(db_path)) as connection:
        rows = connection.execute("""SELECT asset_key, COUNT(*) AS like_count
                                     FROM asset_marks
                                     WHERE asset_kind='inspiration' AND favorite=1
                                     GROUP BY asset_key""").fetchall()
        counts = {}
        for row in rows:
            try:
                key = clean_inspiration_id(row["asset_key"])
            except ValueError:
                continue
            counts[key] = int(row["like_count"])
        result = {"counts": counts}
        if username:
            liked_rows = connection.execute("""SELECT asset_key FROM asset_marks
                                                WHERE username=? AND asset_kind='inspiration' AND favorite=1""",
                                             (username,)).fetchall()
            liked = []
            for row in liked_rows:
                try:
                    liked.append(int(clean_inspiration_id(row["asset_key"])))
                except ValueError:
                    continue
            result["liked"] = sorted(liked)
    return result


def set_like(db_path, username, inspiration_id, favorite):
    key = clean_inspiration_id(inspiration_id)
    if not isinstance(favorite, bool):
        raise ValueError("favorite 必须是布尔值")
    now = int(time.time())
    with closing(_connect(db_path)) as connection:
        connection.execute("""INSERT INTO asset_marks(username,asset_kind,asset_key,favorite,tags,updated_at)
                              VALUES(?,?,?,?,?,?)
                              ON CONFLICT(username,asset_kind,asset_key) DO UPDATE SET
                                favorite=excluded.favorite,
                                tags=excluded.tags,
                                updated_at=excluded.updated_at""",
                           (username, "inspiration", key, 1 if favorite else 0, json.dumps([]), now))
        connection.commit()
        row = connection.execute("""SELECT COUNT(*) AS like_count FROM asset_marks
                                    WHERE asset_kind='inspiration' AND asset_key=? AND favorite=1""",
                                 (key,)).fetchone()
    return {"id": int(key), "favorite": favorite, "count": int(row["like_count"])}


def handle_get(handler, user, db_path):
    username = user.get("username") if user else None
    return handler._send(200, summary(db_path, username))


def handle_post(handler, user, db_path):
    if not user:
        return handler._send(401, {"detail": "未登录"})
    body = handler._json_body()
    try:
        result = set_like(db_path, user["username"], body.get("id"), body.get("favorite"))
        return handler._send(200, {"ok": True, **result})
    except ValueError as error:
        return handler._send(400, {"detail": str(error)[:160]})
