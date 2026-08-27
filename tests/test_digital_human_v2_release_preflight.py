import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/digital_human_v2_release_preflight.py"
sys.path.insert(0, str(ROOT / "server"))
spec = importlib.util.spec_from_file_location("digital_human_v2_release_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class DigitalHumanReleasePreflightTests(unittest.TestCase):
    def test_module_root_ignores_partial_server_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "server/providers").mkdir(parents=True)
            (root / "content_domains").mkdir()
            self.assertEqual(root, preflight._module_root(root))
            (root / "server/content_domains").mkdir()
            self.assertEqual(root / "server", preflight._module_root(root))

    def test_systemd_repeats_versioned_gate_as_service_user(self):
        dropin = (ROOT / (
            "deploy/systemd/huangque-content.service.d/"
            "digital-human-v2-preflight.conf"
        )).read_text("utf-8")
        service = (ROOT / "deploy/systemd/huangque-content.service").read_text("utf-8")
        environment = (ROOT / "deploy/huangque-secrets.env.example").read_text("utf-8")
        self.assertIn("User=ubuntu", service)
        self.assertIn(
            "ExecStartPre=/usr/bin/python3 /home/ubuntu/content-api/scripts/"
            "digital_human_v2_release_preflight.py", dropin,
        )
        self.assertIn(
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT="
            "/home/ubuntu/material-libraries/huangque-media", environment,
        )
        whisper = (ROOT / (
            "deploy/systemd/huangque-content.service.d/whisper.conf"
        )).read_text("utf-8")
        self.assertIn(
            "WHISPER_CACHE_DIR=/home/ubuntu/.cache/huggingface/hub", whisper,
        )
        self.assertIn('Environment="SUBTITLE_FONT=Noto Sans SC"', whisper)

    def _write_mirror(self, raw):
        temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary.name)
        (root / "index.jsonl").write_bytes(raw)
        provenance = {
            "schema_version": 1,
            "source_host": preflight.EXPECTED_LIBRARY_PRIMARY_HOST,
            "source_root": preflight.EXPECTED_LIBRARY_PRIMARY_ROOT,
            "mirror_root": preflight.EXPECTED_LIBRARY_ROOT,
            "entry_count": preflight.EXPECTED_LIBRARY_COUNT,
            "index_sha256": hashlib.sha256(raw).hexdigest(),
        }
        (root / preflight.MIRROR_PROVENANCE_NAME).write_text(
            json.dumps(provenance), encoding="utf-8",
        )
        return temporary, root, provenance

    def test_mirror_provenance_locks_test_server_and_exact_index(self):
        temporary, root, provenance = self._write_mirror(b'{"id":"one"}\n')
        self.addCleanup(temporary.cleanup)
        result = preflight._verify_material_mirror_provenance(root)
        self.assertEqual("8.148.158.106", result["source_host"])
        self.assertEqual(provenance["index_sha256"], result["index_sha256"])

    def test_mirror_index_drift_fails_closed(self):
        temporary, root, _provenance = self._write_mirror(b'{"id":"one"}\n')
        self.addCleanup(temporary.cleanup)
        (root / "index.jsonl").write_bytes(b'{"id":"two"}\n')
        with self.assertRaisesRegex(preflight.PreflightError, "does not match"):
            preflight._verify_material_mirror_provenance(root)

    def test_mirror_from_another_host_fails_closed(self):
        temporary, root, provenance = self._write_mirror(b'{"id":"one"}\n')
        self.addCleanup(temporary.cleanup)
        provenance["source_host"] = "127.0.0.1"
        (root / preflight.MIRROR_PROVENANCE_NAME).write_text(
            json.dumps(provenance), encoding="utf-8",
        )
        with self.assertRaisesRegex(preflight.PreflightError, "source is not locked"):
            preflight._verify_material_mirror_provenance(root)

    def test_all_locked_dependencies_are_checked(self):
        from content_domains import core, digital_human_v2, video

        provider = mock.Mock()
        provider.heygen_upload_preflight.return_value = {"ok": True, "no_charge": True}
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": preflight.EXPECTED_LIBRARY_ROOT,
        }, clear=False), mock.patch.object(
            preflight, "_verify_material_mirror_provenance",
            return_value={"index_sha256": "a" * 64, "source_host": "8.148.158.106"},
        ) as mirror, mock.patch.object(
            digital_human_v2, "local_material_library_operational_probe",
            return_value={"count": 204, "verified_files": 204},
        ) as library, mock.patch.object(
            video, "subtitle_runtime_preflight",
            return_value={"ok": True, "model": "small"},
        ) as subtitle, mock.patch.object(core, "_domains", return_value=(None, None, provider)):
            result = preflight.run()
        self.assertTrue(result["ok"])
        self.assertEqual("a" * 64, result["library_index_sha256"])
        self.assertEqual(2, mirror.call_count)
        library.assert_called_once_with(204, verify_all=True)
        subtitle.assert_called_once_with()
        provider.heygen_upload_preflight.assert_called_once_with()

    def test_unlocked_library_root_fails_before_any_probe(self):
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": "/tmp/materials",
        }, clear=False), self.assertRaisesRegex(preflight.PreflightError, "not locked"):
            preflight.run()

    def test_runtime_configuration_must_match_observed_production(self):
        from content_domains import video

        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": preflight.EXPECTED_LIBRARY_ROOT,
        }, clear=False), mock.patch.object(
            video, "WHISPER_CACHE_DIR", "/var/cache/huangque/faster-whisper",
        ), self.assertRaisesRegex(preflight.PreflightError, "production locked"):
            preflight.run()

    def test_heygen_not_ready_fails_closed(self):
        from content_domains import core, digital_human_v2, video

        provider = mock.Mock()
        provider.heygen_upload_preflight.return_value = {"ok": False}
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": preflight.EXPECTED_LIBRARY_ROOT,
        }, clear=False), mock.patch.object(
            preflight, "_verify_material_mirror_provenance",
            return_value={"index_sha256": "a" * 64, "source_host": "8.148.158.106"},
        ), mock.patch.object(
            digital_human_v2, "local_material_library_operational_probe",
            return_value={"count": 204, "verified_files": 204},
        ), mock.patch.object(
            video, "subtitle_runtime_preflight",
            return_value={"ok": True, "model": "small"},
        ), mock.patch.object(core, "_domains", return_value=(None, None, provider)), \
                self.assertRaisesRegex(preflight.PreflightError, "HeyGen"):
            preflight.run()

    def test_video_upload_preflight_never_creates_an_asset(self):
        from content_domains import video

        with mock.patch.object(video, "_heygen_mcp_enabled", return_value=True), \
             mock.patch.object(video, "_heygen_mcp_access_token", return_value="token"), \
             mock.patch.object(video, "_heygen_upload_asset_oauth") as upload:
            result = video.heygen_upload_preflight()
        self.assertEqual("oauth", result["upload_auth"])
        self.assertTrue(result["no_charge"])
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
