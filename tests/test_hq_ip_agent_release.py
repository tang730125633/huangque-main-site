"""Offline release fault injection; all targets are temporary fixtures."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hq_release", ROOT / "scripts/hq_ip_agent_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live, self.bundle, self.backup = (self.root / n for n in ("live", "bundle", "backup"))
        self.manifest = json.loads((ROOT / release.MANIFEST).read_text(encoding="utf-8"))
        for row in self.manifest["files"]:
            for base, phase in ((self.live, "before"), (self.bundle, "after")):
                target = base / row["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(("# fixture " + phase + "\n").encode())
                row[phase] = release.digest(target)
        self.actions = []

    def assert_phase(self, phase):
        for row in self.manifest["files"]:
            self.assertEqual(release.digest(self.live / row["target"]), row[phase])

    def install(self, health=None):
        return release.install(self.bundle, self.live, self.backup, self.manifest,
                               self.actions.append, health or self.assert_phase)

    def test_repository_manifest_matches_git_sources(self):
        manifest = json.loads((ROOT / release.MANIFEST).read_text(encoding="utf-8"))
        for row in release.validate(manifest):
            self.assertEqual(release.digest(ROOT / row["source"]), row["after"])

    def test_success_changes_only_declared_files_and_keeps_backup(self):
        untouched = self.live / "unrelated.txt"
        untouched.write_text("preserve")
        self.install()
        self.assert_phase("after")
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(untouched.read_text(), "preserve")
        for row in self.manifest["files"]:
            self.assertEqual(release.digest(self.backup / row["target"]), row["before"])

    def test_tampered_bundle_does_not_stop_or_backup(self):
        (self.bundle / "app.py").write_text("# wrong")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.actions, [])
        self.assertFalse(self.backup.exists())
        self.assert_phase("before")

    def test_production_drift_stops_before_any_change(self):
        (self.live / "app.py").write_text("# concurrent deployment")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.actions, [])
        self.assertFalse(self.backup.exists())

    def test_mid_install_failure_restores_all_original_files(self):
        original = release.atomic_copy
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replace failure")
            original(source, target)
        with patch.object(release, "atomic_copy", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "original files restored"):
                self.install()
        self.assert_phase("before")
        self.assertEqual(self.actions, ["stop", "stop", "start"])

    def test_served_static_or_health_failure_restores_old_version(self):
        def health(phase):
            self.assert_phase(phase)
            if phase == "after":
                raise RuntimeError("injected HTTP hash mismatch")
        with self.assertRaisesRegex(RuntimeError, "original files restored"):
            self.install(health)
        self.assert_phase("before")
        self.assertEqual(self.actions, ["stop", "start", "stop", "start"])

    def test_repeated_release_never_reapplies_on_new_version(self):
        self.install()
        self.actions.clear()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.actions, [])

    def test_cannot_expand_targets_to_hermes_or_environment(self):
        for target in ("../hermes-web/server.py", ".env", "data/session.json"):
            manifest = copy.deepcopy(self.manifest)
            manifest["files"][0]["target"] = target
            with self.assertRaises(ValueError):
                release.validate(manifest)

    def test_wrong_source_mapping_rejected(self):
        self.manifest["files"][0]["source"] = "server/hermes_ip12/server.py"
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.actions, [])


if __name__ == "__main__":
    unittest.main()
