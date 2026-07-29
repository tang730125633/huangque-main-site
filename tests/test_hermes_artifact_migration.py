import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import migrate_hermes_artifacts


class HermesArtifactMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "hermes"
        self.data = self.root / "data"
        self.legacy_media = self.root / "media_library"
        self.legacy_media.mkdir(parents=True)

        indexed = self.legacy_media / "people" / "portrait.jpg"
        indexed.parent.mkdir()
        indexed.write_bytes(b"portrait")
        orphan = self.legacy_media / "clips" / "intro.mp4"
        orphan.parent.mkdir()
        orphan.write_bytes(b"clip")
        (self.legacy_media / "index.json").write_text(
            json.dumps(
                {
                    "entries": {
                        "people_1": {
                            "id": "people_1",
                            "keyword": "people",
                            "file_path": str(indexed),
                            "original_name": indexed.name,
                            "source": "manual",
                            "tags": ["people"],
                            "use_count": 2,
                        }
                    },
                    "keywords": {"people": ["people_1"]},
                }
            ),
            encoding="utf-8",
        )

        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        (knowledge / "keyword_map.json").write_text(
            '{"人物":{"english":"person"}}', encoding="utf-8"
        )
        videos = self.data / "videos"
        videos.mkdir(parents=True)
        (videos / "0123456789.mp4").write_bytes(b"video")
        (videos / "replica_abcdef0123.mp4").write_bytes(b"replica")
        analysis = self.data / "analyses" / "0123456789"
        analysis.mkdir(parents=True)
        (analysis / "result.json").write_text('{"ok":true}', encoding="utf-8")
        uploads = self.data / "uploads"
        uploads.mkdir()
        (uploads / "abcdef0123.mp4").write_bytes(b"upload")

    def tearDown(self):
        self.temp.cleanup()

    def test_copy_only_migration_is_idempotent_and_rollback_safe(self):
        dry_run = migrate_hermes_artifacts.migrate(
            self.root, self.data, "legacy-user", dry_run=True
        )
        self.assertEqual(dry_run["state"], "dry-run")
        self.assertFalse((self.data / ".migrations").exists())

        result = migrate_hermes_artifacts.migrate(
            self.root, self.data, "legacy-user"
        )
        self.assertEqual(result["state"], "completed")
        owner_key = result["owner_key"]
        owner_root = self.data / "users" / owner_key

        self.assertTrue((owner_root / "videos" / "0123456789.mp4").is_file())
        self.assertTrue(
            (owner_root / "videos" / "replica_abcdef0123.mp4").is_file()
        )
        self.assertTrue(
            (owner_root / "analyses" / "0123456789" / "result.json").is_file()
        )
        self.assertTrue((owner_root / "uploads" / "abcdef0123.mp4").is_file())
        self.assertTrue((self.data / "knowledge" / "keyword_map.json").is_file())

        index = json.loads(
            (self.data / "media_library" / "index.json").read_text(encoding="utf-8")
        )
        migrated = list(index["entries"].values())
        self.assertEqual(len(migrated), 2)
        self.assertEqual({entry["owner_username"] for entry in migrated}, {"legacy-user"})
        self.assertTrue(
            all(
                Path(entry["file_path"]).is_relative_to(
                    self.data / "media_library" / owner_key
                )
                for entry in migrated
            )
        )

        # Migration never removes the old tree, so rollback/fallback code can use it.
        self.assertTrue((self.root / "media_library" / "people" / "portrait.jpg").is_file())
        self.assertTrue((self.data / "videos" / "0123456789.mp4").is_file())

        repeated = migrate_hermes_artifacts.migrate(
            self.root, self.data, "legacy-user"
        )
        self.assertEqual(repeated["completed_at"], result["completed_at"])

        rolled_back = migrate_hermes_artifacts.rollback(self.data)
        self.assertEqual(rolled_back["state"], "rolled-back")
        self.assertFalse((self.data / "media_library" / "index.json").exists())
        self.assertFalse((owner_root / "videos" / "0123456789.mp4").exists())
        self.assertFalse((self.data / "knowledge" / "keyword_map.json").exists())
        self.assertTrue((self.data / "videos" / "0123456789.mp4").is_file())

    def test_rollback_refuses_to_overwrite_post_migration_index_changes(self):
        migrate_hermes_artifacts.migrate(self.root, self.data, "legacy-user")
        index_path = self.data / "media_library" / "index.json"
        index_path.write_text('{"entries":{"new":{}},"keywords":{}}', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "changed after migration"):
            migrate_hermes_artifacts.rollback(self.data)
        self.assertTrue(index_path.exists())

    def test_near_quota_preflight_fails_before_any_migration_write(self):
        plan = migrate_hermes_artifacts.build_plan(
            self.root, self.data, "legacy-user", quota_bytes=1024 * 1024
        )
        projected = plan["quota"]["projected_bytes"]
        self.assertGreater(projected, 0)

        with self.assertRaisesRegex(
            migrate_hermes_artifacts.QuotaPreflightError,
            "set HERMES_DATA_QUOTA_MB",
        ):
            migrate_hermes_artifacts.migrate(
                self.root,
                self.data,
                "legacy-user",
                quota_bytes=max(1, projected // 2),
            )
        self.assertFalse((self.data / ".migrations").exists())
        self.assertFalse((self.data / "users").exists())

    def test_retained_legacy_files_are_excluded_from_canonical_quota(self):
        result = migrate_hermes_artifacts.migrate(
            self.root, self.data, "legacy-user", quota_bytes=1024 * 1024
        )
        after_migration = migrate_hermes_artifacts._quota_directory_size(self.data)
        self.assertEqual(after_migration, result["quota"]["projected_bytes"])

        retained = self.data / "videos" / "0123456789.mp4"
        retained.write_bytes(retained.read_bytes() + b"x" * 10000)
        self.assertEqual(
            migrate_hermes_artifacts._quota_directory_size(self.data),
            after_migration,
        )

        active = self.data / "agnes_lab" / "images" / "active.png"
        active.parent.mkdir(parents=True)
        active.write_bytes(b"active-data")
        self.assertEqual(
            migrate_hermes_artifacts._quota_directory_size(self.data),
            after_migration + len(b"active-data"),
        )

    def test_runtime_quota_excludes_legacy_but_charges_canonical_move(self):
        runtime_data = self.root / "runtime-data"
        script = r"""
from pathlib import Path
import artifact_store

legacy = artifact_store.DATA_DIR / "videos" / "legacy.bin"
legacy.parent.mkdir(parents=True)
legacy.write_bytes(b"0123456789")
assert artifact_store.directory_size() == 0

destination = artifact_store.media_path(
    "legacy-user", artifact_store.new_asset_id(), ".bin"
)
artifact_store.DATA_QUOTA_BYTES = 5
try:
    artifact_store.finalize_file(legacy, destination)
    raise AssertionError("legacy-to-canonical move bypassed quota")
except artifact_store.StorageQuotaExceeded:
    pass
assert legacy.exists()
assert not destination.exists()
print("RUNTIME_QUOTA_OK")
"""
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.root)
        env["HERMES_DATA_DIR"] = str(runtime_data)
        env["PYTHONPATH"] = str(
            Path(__file__).parents[1] / "server" / "hermes_ip12"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=Path(__file__).parents[1],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RUNTIME_QUOTA_OK", result.stdout)

    def test_prepared_manifest_resumes_without_losing_rollback_ownership(self):
        plan = migrate_hermes_artifacts.build_plan(
            self.root, self.data, "legacy-user"
        )
        manifest = Path(plan["manifest_path"])
        migrate_hermes_artifacts._atomic_write(
            manifest, migrate_hermes_artifacts._json_bytes(plan)
        )
        first = plan["operations"][0]
        destination = Path(first["destination"])
        destination.parent.mkdir(parents=True)
        destination.write_bytes(Path(first["source"]).read_bytes())

        completed = migrate_hermes_artifacts.migrate(
            self.root, self.data, "legacy-user"
        )
        self.assertEqual(completed["state"], "completed")
        self.assertTrue(first["created"])
        migrate_hermes_artifacts.rollback(self.data)
        self.assertFalse(destination.exists())

    def test_owner_is_required_and_destination_conflicts_abort_before_copy(self):
        with self.assertRaisesRegex(ValueError, "legacy owner"):
            migrate_hermes_artifacts.build_plan(self.root, self.data, "")

        owner_key = migrate_hermes_artifacts._owner_key("legacy-user")
        conflict = (
            self.data
            / "media_library"
            / owner_key
            / "legacy"
            / "people"
            / "portrait.jpg"
        )
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"different")
        with self.assertRaisesRegex(FileExistsError, "destination conflict"):
            migrate_hermes_artifacts.migrate(self.root, self.data, "legacy-user")
        self.assertFalse((self.data / ".migrations").exists())


if __name__ == "__main__":
    unittest.main()
