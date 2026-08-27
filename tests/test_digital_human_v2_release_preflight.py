import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/digital_human_v2_release_preflight.py"
sys.path.insert(0, str(ROOT / "server"))
spec = importlib.util.spec_from_file_location("digital_human_v2_release_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class DigitalHumanReleasePreflightTests(unittest.TestCase):
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

    def test_all_locked_dependencies_are_checked(self):
        from content_domains import core, digital_human_v2, video

        provider = mock.Mock()
        provider.heygen_upload_preflight.return_value = {"ok": True, "no_charge": True}
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": preflight.EXPECTED_LIBRARY_ROOT,
        }, clear=False), mock.patch.object(
            digital_human_v2, "local_material_library_operational_probe",
            return_value={"count": 204},
        ) as library, mock.patch.object(
            video, "subtitle_runtime_preflight",
            return_value={"ok": True, "model": "small"},
        ) as subtitle, mock.patch.object(core, "_domains", return_value=(None, None, provider)):
            result = preflight.run()
        self.assertTrue(result["ok"])
        library.assert_called_once_with(204)
        subtitle.assert_called_once_with()
        provider.heygen_upload_preflight.assert_called_once_with()

    def test_unlocked_library_root_fails_before_any_probe(self):
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": "/tmp/materials",
        }, clear=False), self.assertRaisesRegex(preflight.PreflightError, "not locked"):
            preflight.run()

    def test_heygen_not_ready_fails_closed(self):
        from content_domains import core, digital_human_v2, video

        provider = mock.Mock()
        provider.heygen_upload_preflight.return_value = {"ok": False}
        with mock.patch.dict(os.environ, {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": preflight.EXPECTED_LIBRARY_ROOT,
        }, clear=False), mock.patch.object(
            digital_human_v2, "local_material_library_operational_probe",
            return_value={"count": 204},
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
