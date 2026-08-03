import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

try:
    from tests.fixtures.short_drama_voice_acceptance import build_acceptance_fixture
except ModuleNotFoundError:
    from fixtures.short_drama_voice_acceptance import build_acceptance_fixture


class ShortDramaVoiceAcceptanceFixtureTests(unittest.TestCase):
    def test_rejects_repository_database_paths(self):
        repository_db = Path(__file__).resolve().parents[1] / "server" / "content_jobs.db"
        with self.assertRaises(ValueError):
            build_acceptance_fixture(repository_db, repository_db.with_name("users.db"))

    def test_builds_isolated_six_shot_voice_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content_db = root / "content.db"
            auth_db = root / "auth.db"
            fixture = build_acceptance_fixture(content_db, auth_db)

            self.assertEqual(5, len(fixture["voice_line_ids"]))
            self.assertEqual(
                {"project_id", "board_id", "owner", "viewer", "unauthorized",
                 "passwords", "voice_line_ids"},
                set(fixture),
            )
            with closing(sqlite3.connect(content_db)) as conn:
                self.assertEqual(6, conn.execute(
                    "SELECT COUNT(*) FROM short_drama_voice_shots WHERE project_id=?",
                    (fixture["project_id"],),
                ).fetchone()[0])
                self.assertEqual(0, conn.execute(
                    "SELECT COUNT(*) FROM short_drama_voice_jobs"
                ).fetchone()[0])
            with closing(sqlite3.connect(auth_db)) as conn:
                roles = dict(conn.execute(
                    "SELECT username,role FROM canvas_members WHERE board_id=?",
                    (fixture["board_id"],),
                ).fetchall())
                self.assertEqual("editor", roles[fixture["owner"]])
                self.assertEqual("viewer", roles[fixture["viewer"]])
                self.assertNotIn(fixture["unauthorized"], roles)


if __name__ == "__main__":
    unittest.main()
