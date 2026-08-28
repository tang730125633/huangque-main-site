import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import dl_service
from content_domains import core, cos


class PrivateAssetsTest(unittest.TestCase):
    PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def test_local_provider_reference_url_is_signed_and_expires(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"
                ), \
                patch.object(
                    core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32
                ), \
                patch.object(core, "LOCAL_FILE_URL_TTL", 3600):
            reference = Path(tmp) / "short_drama_role_uploads" / "role one.png"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(self.PNG_1X1)

            url = core.local_provider_reference_url(
                "short_drama_role_uploads/role one.png", now=1000
            )
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)

            self.assertEqual("https", parsed.scheme)
            self.assertEqual("media.example", parsed.netloc)
            self.assertEqual(
                "/api/gen/file/short_drama_role_uploads/role%20one.png",
                parsed.path,
            )
            self.assertTrue(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=1000
            ))
            self.assertFalse(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=4601
            ))
            query["hq_sig"] = ["0" * 64]
            self.assertFalse(core._valid_local_provider_file_signature(
                "short_drama_role_uploads/role one.png", query, now=1000
            ))

    def test_local_provider_reference_url_requires_public_https_and_image(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32):
            source = Path(tmp) / "reference.txt"
            source.write_text("not an image", encoding="utf-8")
            with patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "http://media.example"):
                with self.assertRaises(RuntimeError):
                    core.local_provider_reference_url("reference.txt")
            for invalid_origin in (
                "https://user:password@media.example",
                "https://media.example/prefix",
                "https://localhost",
                "https://127.0.0.1",
            ):
                with self.subTest(origin=invalid_origin), patch.object(
                        core, "LOCAL_FILE_PUBLIC_BASE_URL", invalid_origin):
                    with self.assertRaises(RuntimeError):
                        core.local_provider_reference_url("reference.txt")
            with patch.object(
                    core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"):
                with self.assertRaises(ValueError):
                    core.local_provider_reference_url("reference.txt")

    def test_local_provider_reference_url_rejects_fake_or_mismatched_images(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32):
            fake = Path(tmp) / "short_drama_role_uploads" / "fake.png"
            fake.parent.mkdir(parents=True)
            fake.write_bytes(b"png")
            with self.assertRaisesRegex(ValueError, "valid JPG, PNG, or WebP"):
                core.local_provider_reference_url(fake.relative_to(tmp).as_posix())

            mismatched = fake.with_name("mismatched.jpg")
            mismatched.write_bytes(self.PNG_1X1)
            with self.assertRaisesRegex(ValueError, "valid JPG, PNG, or WebP"):
                core.local_provider_reference_url(mismatched.relative_to(tmp).as_posix())

    def test_local_provider_configuration_uses_dedicated_secret_and_ttl_precedence(self):
        self.assertEqual(
            "dedicated",
            core._local_file_signing_secret({
                "HQ_LOCAL_FILE_SIGNING_SECRET": "dedicated",
                "HQ_INTERNAL_TOKEN": "must-not-be-reused",
            }),
        )
        self.assertEqual(
            "",
            core._local_file_signing_secret({"HQ_INTERNAL_TOKEN": "must-not-be-reused"}),
        )
        self.assertEqual(
            7200,
            core._local_file_url_ttl({
                "HQ_LOCAL_FILE_URL_TTL_SECONDS": "7200",
                "HQ_LOCAL_FILE_URL_TTL": "120",
            }),
        )
        self.assertEqual(120, core._local_file_url_ttl({"HQ_LOCAL_FILE_URL_TTL": "120"}))

    def test_production_contract_documents_local_reference_signing(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "deploy" / "huangque-secrets.env.example").read_text(
            encoding="utf-8"
        )
        runbook = (root / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "HQ_CONTENT_PUBLIC_BASE_URL",
            "HQ_LOCAL_FILE_SIGNING_SECRET",
            "HQ_LOCAL_FILE_URL_TTL_SECONDS",
        ):
            self.assertIn(name, example)
            self.assertIn(name, runbook)
        for requirement in (
            "openssl rand",
            "chmod 600",
            "huangque-content",
            "篡改",
            "过期",
            "回滚",
        ):
            self.assertIn(requirement, runbook)

    def test_production_contract_documents_grok_provider_selection(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "deploy" / "huangque-secrets.env.example").read_text(
            encoding="utf-8"
        )
        runbook = (root / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER=grok", example)
        self.assertIn("HQ_SHORT_DRAMA_GROK_MODEL=", example)
        for name in (
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER",
            "HQ_SHORT_DRAMA_GROK_MODEL",
        ):
            self.assertIn(name, runbook)
        for requirement in (
            "grok_xai",
            "minimax_h3",
            "XAI_API_KEY",
            "/home/ubuntu/content-api/content.env",
            "huangque-content",
            "逐镜",
            "预览",
            "冒烟",
            "回滚",
        ):
            self.assertIn(requirement, runbook)

    def test_production_contract_covers_native_2k_formal_delivery(self):
        root = Path(__file__).resolve().parents[1]
        runbook = (root / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        transition_path = root / "deploy" / "formal-delivery-live-transition.sh"
        self.assertTrue(transition_path.is_file())
        transition = transition_path.read_text(encoding="utf-8")
        production_contract = runbook + "\n" + transition
        manifest_path = root / "deploy" / "formal-delivery-release-manifest.tsv"
        self.assertTrue(manifest_path.is_file())
        manifest_rows = [
            tuple(line.split("\t"))
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(all(len(row) == 2 for row in manifest_rows))
        manifest = dict(manifest_rows)
        expected_sources = {
            "deploy/systemd/huangque-content.service.d/formal-delivery.conf",
            "server/admin_api.py",
            "server/content_domains/core.py",
            "server/content_domains/pricing.py",
            "server/content_domains/short_drama.py",
            "server/content_domains/short_drama_asset_graph.py",
            "server/content_domains/short_drama_autodraft.py",
            "server/content_domains/short_drama_formal_renderer.py",
            "server/content_domains/short_drama_native_audio.py",
            "server/content_domains/short_drama_refinement.py",
            "server/content_domains/video.py",
            "server/content_domains/video_minimax_h3.py",
            "server/providers/short_drama_visual/base.py",
            "server/providers/short_drama_visual/grok_xai.py",
            "server/providers/short_drama_visual/heygen_cinematic.py",
            "server/providers/short_drama_visual/minimax_h3.py",
            "site/workbench/short-drama-workspace.css",
            "site/workbench/short-drama-workspace.js",
            "site/workbench/short-drama.html",
        }
        self.assertEqual(set(manifest), expected_sources)
        self.assertEqual(len(manifest_rows), len(expected_sources))
        self.assertEqual(
            len({target for _source, target in manifest_rows}),
            len(manifest_rows),
        )
        for source, target in manifest_rows:
            self.assertTrue((root / source).is_file(), source)
            self.assertFalse(source.endswith("/"), source)
            self.assertTrue(target.startswith("/"), target)
            self.assertFalse(target.endswith("/"), target)
            self.assertNotIn("..", source.split("/"), source)
            self.assertNotIn("..", target.split("/"), target)
            if source.startswith("server/"):
                expected_target = "/home/ubuntu/content-api/" + source[7:]
            elif source.startswith("site/workbench/"):
                expected_target = (
                    "/var/www/huangquechuanmei/workbench/" + source.rsplit("/", 1)[1]
                )
            else:
                expected_target = (
                    "/etc/systemd/system/huangque-content.service.d/"
                    "formal-delivery.conf"
                )
            self.assertEqual(expected_target, target, source)
        drop_in = (
            root / "deploy" / "systemd" / "huangque-content.service.d" /
            "formal-delivery.conf"
        ).read_text(encoding="utf-8")
        for module in (
            "providers/__init__.py",
            "providers/short_drama_visual/__init__.py",
            "providers/short_drama_visual/runtime.py",
            "server/func_names.py",
            "server/inspiration_cases.py",
            "server/tikhub.py",
        ):
            self.assertIn(module, runbook)
        for requirement in (
            "HQ_RELEASE_COMMIT",
            'git merge-base --is-ancestor "$HQ_RELEASE_COMMIT" origin/main',
            "HQ_RELEASE_STAGE",
            "HQ_RELEASE_MANIFEST",
            "release-staging/formal-delivery-",
            "python3 -m compileall -q",
            "short_drama_refinement",
            "short_drama_formal_renderer",
            "import admin_api",
            "git rev-parse HEAD",
            "ffmpeg -version",
            "ffprobe -version",
            "libx264",
            "aac",
            "CONTENT_OUT",
            "systemctl restart huangque-content",
            "systemctl restart huangque-content huangque-admin",
            "/api/gen/health",
            "short_drama_delivery",
            "mkdir -p",
            "2560x1440",
            "release-backup",
            "set -euo pipefail",
            "states.tsv",
            "printf '%s\\t%s\\t%s\\n'",
            "short-drama-workspace.js",
            '-o "$HQ_HTTPS_DIR/workspace.js"',
            "HQ_EXPECT_JS_SHA",
            "HQ_EXPECT_CSS_SHA",
            "HQ_EXPECT_HTML_SHA",
            "stamp_assets.py --check",
            "回滚",
        ):
            self.assertIn(requirement, production_contract)
        self.assertNotIn(
            "dapeng-server:/home/ubuntu/content-api/content_domains/", runbook,
        )
        for forbidden in (
            "--delete-excluded",
            "sudo rsync -a --delete",
            "sudo rm -rf /home/ubuntu/content-api/content_domains",
            "server/content_domains/ \\",
            "server/providers/ \\",
        ):
            self.assertNotIn(forbidden, production_contract)
        staged_import = runbook.index(
            'cd "$HQ_PREFLIGHT/content-api"'
        )
        transition_call = runbook.index(
            'bash "$HQ_RELEASE_STAGE/live-transition.sh"'
        )
        rollback_armed = transition.index("HQ_ACTIVATED=1")
        first_live_install = transition.index(
            'sudo install -D -m 0644 "$HQ_RELEASE_STAGE/files/$HQ_SOURCE"'
        )
        self.assertLess(staged_import, transition_call)
        self.assertLess(rollback_armed, first_live_install)
        automatic_rollback = transition[
            transition.index("finish_release() {"):
            transition.index("trap finish_release EXIT")
        ]
        automatic_stop = automatic_rollback.index(
            "sudo systemctl stop huangque-content huangque-admin"
        )
        automatic_restore = automatic_rollback.index(
            "if restore_release_manifest; then"
        )
        automatic_restart = automatic_rollback.index(
            "sudo systemctl restart huangque-content huangque-admin"
        )
        self.assertLess(automatic_stop, automatic_restore)
        self.assertLess(automatic_restore, automatic_restart)
        rollback_failure = automatic_rollback.index(
            "    else\n"
            "      echo 'CRITICAL: formal-delivery manifest rollback failed; "
            "services remain stopped' >&2"
        )
        rollback_finished = automatic_rollback.index("    fi\n", rollback_failure)
        self.assertIn(
            "sudo systemctl restart huangque-content huangque-admin",
            automatic_rollback[automatic_restore:rollback_failure],
        )
        self.assertNotIn("systemctl restart", automatic_rollback[rollback_failure:])
        self.assertIn("exit 1", automatic_rollback[rollback_failure:rollback_finished])
        manual_rollback = runbook[
            runbook.index("<<'REMOTE_ROLLBACK'"):runbook.index("REMOTE_ROLLBACK\n")
        ]
        manual_fail_fast = manual_rollback.index("set -euo pipefail")
        manual_stop = manual_rollback.index(
            "sudo systemctl stop huangque-content huangque-admin"
        )
        manual_restore = manual_rollback.index(
            "while IFS=$'\\t' read -r HQ_SOURCE HQ_TARGET HQ_STATE; do"
        )
        manual_restart = manual_rollback.index(
            "sudo systemctl restart huangque-content huangque-admin"
        )
        self.assertLess(manual_fail_fast, manual_stop)
        self.assertLess(manual_stop, manual_restore)
        self.assertLess(manual_restore, manual_restart)
        self.assertIn("原生 2K", drop_in)
        self.assertNotIn("1080p", drop_in)

    def _run_formal_delivery_transition(self, public_mode="success", rollback_fail=False):
        root = Path(__file__).resolve().parents[1]
        script = root / "deploy" / "formal-delivery-live-transition.sh"
        bash = os.environ.get("HQ_TEST_BASH") or shutil.which("bash")
        if not bash:
            self.skipTest("bash is required for the deployment transition contract")
        with tempfile.TemporaryDirectory() as tmp:
            native_root = Path(tmp)
            if os.name == "nt":
                converted = subprocess.run(
                    [
                        bash, "-lc",
                        'cygpath -u "$HQ_NATIVE_TMP"; cygpath -u "$HQ_NATIVE_SCRIPT"',
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=dict(
                        os.environ,
                        HQ_NATIVE_TMP=str(native_root),
                        HQ_NATIVE_SCRIPT=str(script),
                    ),
                )
                shell_root, shell_script = converted.stdout.splitlines()
            else:
                shell_root = native_root.as_posix()
                shell_script = script.as_posix()
            stage = native_root / "stage"
            backup = native_root / "backup"
            web = native_root / "web"
            fake_bin = native_root / "fake-bin"
            stage.mkdir()
            backup.mkdir()
            web.mkdir()
            fake_bin.mkdir()
            event_log = native_root / "events.log"
            curl_log = native_root / "curl.log"
            shell_stage = shell_root + "/stage"
            shell_backup = shell_root + "/backup"
            shell_web = shell_root + "/web"
            sources = {
                "site/workbench/short-drama-workspace.js": "new-js\n",
                "site/workbench/short-drama-workspace.css": "new-css\n",
                "site/workbench/short-drama.html": "new-html\n",
            }
            targets = {}
            for source, content in sources.items():
                name = source.rsplit("/", 1)[1]
                source_path = stage / "files" / Path(*source.split("/"))
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(content, encoding="utf-8", newline="\n")
                target = shell_web + "/" + name
                targets[source] = target
                (web / name).write_text("old-" + name + "\n", encoding="utf-8", newline="\n")
            manifest_lines = [
                source + "\t" + targets[source] for source in sources
            ]
            (stage / "release-manifest.tsv").write_text(
                "\n".join(manifest_lines) + "\n", encoding="utf-8", newline="\n"
            )
            states = []
            for source, target in targets.items():
                relative_target = target.lstrip("/")
                backup_file = backup / "files" / Path(*relative_target.split("/"))
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                backup_file.write_bytes((web / source.rsplit("/", 1)[1]).read_bytes())
                states.append(source + "\t" + target + "\tpresent")
            (backup / "states.tsv").write_text(
                "\n".join(states) + "\n", encoding="utf-8", newline="\n"
            )
            fake_commands = {
                "sudo": """#!/usr/bin/env bash
if test "${HQ_TEST_ROLLBACK_FAIL:-0}" = 1 && test "$1" = cp && [[ "$*" == *"$HQ_RELEASE_BACKUP/files/"* ]]; then
  exit 73
fi
exec "$@"
""",
                "systemctl": """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >>"$HQ_TEST_EVENT_LOG"
exit 0
""",
                "curl": """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$HQ_TEST_CURL_LOG"
printf 'curl %s\\n' "$*" >>"$HQ_TEST_EVENT_LOG"
case " $* " in *' --connect-timeout '*) ;; *) exit 90 ;; esac
case " $* " in *' --max-time '*) ;; *) exit 91 ;; esac
url=''
output=''
while test "$#" -gt 0; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    http://*|https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  "$HQ_PUBLIC_BASE_URL"/*)
    if test "$HQ_TEST_PUBLIC_MODE" = fail; then exit 22; fi
    cp "$HQ_WEB_ROOT/${url##*/}" "$output"
    ;;
  *) printf 'ok\\n' ;;
esac
""",
            }
            for name, content in fake_commands.items():
                command = fake_bin / name
                command.write_text(content, encoding="utf-8", newline="\n")
                command.chmod(0o755)
            expected = {
                source: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for source, content in sources.items()
            }
            env = dict(os.environ)
            env.update({
                "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                "HQ_RELEASE_STAGE": shell_stage,
                "HQ_RELEASE_BACKUP": shell_backup,
                "HQ_WEB_ROOT": shell_web,
                "HQ_CONTENT_ROOT": shell_root + "/content",
                "HQ_SYSTEMD_TARGET": shell_root + "/formal-delivery.conf",
                "HQ_PUBLIC_BASE_URL": "https://public.example/workbench",
                "HQ_CONTENT_HEALTH_URL": "http://127.0.0.1:18096/health",
                "HQ_ADMIN_HEALTH_URL": "http://127.0.0.1:18098/health",
                "HQ_EXPECT_JS_SHA": expected["site/workbench/short-drama-workspace.js"],
                "HQ_EXPECT_CSS_SHA": expected["site/workbench/short-drama-workspace.css"],
                "HQ_EXPECT_HTML_SHA": expected["site/workbench/short-drama.html"],
                "HQ_TEST_EVENT_LOG": shell_root + "/events.log",
                "HQ_TEST_CURL_LOG": shell_root + "/curl.log",
                "HQ_TEST_PUBLIC_MODE": public_mode,
                "HQ_TEST_ROLLBACK_FAIL": "1" if rollback_fail else "0",
                "HQ_TEST_FAKE_BIN": shell_root + "/fake-bin",
            })
            boundary_probe = subprocess.run(
                [
                    bash, "-c",
                    'PATH="$HQ_TEST_FAKE_BIN:$PATH"; export PATH; command -v curl',
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                env=env,
            )
            if "/fake-bin/curl" not in boundary_probe.stdout.replace("\\", "/"):
                self.fail(
                    "fake curl boundary was not selected: "
                    + boundary_probe.stdout + boundary_probe.stderr
                )
            result = subprocess.run(
                [
                    bash, "-c",
                    'PATH="$HQ_TEST_FAKE_BIN:$PATH"; export PATH; exec bash "$1"',
                    "formal-delivery-transition", shell_script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=env,
            )
            events = (
                event_log.read_text(encoding="utf-8").splitlines()
                if event_log.exists() else []
            )
            curls = (
                curl_log.read_text(encoding="utf-8").splitlines()
                if curl_log.exists() else []
            )
            live = {path.name: path.read_bytes() for path in web.iterdir()}
            return result, events, curls, live

    def test_formal_delivery_network_checks_are_bounded_after_service_restart(self):
        result, events, curls, _live = self._run_formal_delivery_transition()

        self.assertEqual(0, result.returncode, result.stderr)
        restart = events.index(
            "systemctl restart huangque-content huangque-admin"
        )
        first_public = next(
            index for index, event in enumerate(events)
            if "https://public.example/" in event
        )
        active_checks = [
            events.index("systemctl is-active --quiet huangque-content"),
            events.index("systemctl is-active --quiet huangque-admin"),
        ]
        local_health_checks = [
            next(
                index for index, event in enumerate(events)
                if "http://127.0.0.1:18096/health" in event
            ),
            next(
                index for index, event in enumerate(events)
                if "http://127.0.0.1:18098/health" in event
            ),
        ]
        self.assertLess(restart, min(active_checks))
        self.assertLess(max(active_checks), min(local_health_checks))
        self.assertLess(max(local_health_checks), first_public)
        self.assertTrue(curls)
        for command in curls:
            self.assertIn("--connect-timeout 5", command)
            self.assertIn("--max-time 20", command)

    def test_formal_delivery_public_failure_rolls_back_and_restarts_old_release(self):
        result, events, _curls, live = self._run_formal_delivery_transition(
            public_mode="fail"
        )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            2,
            events.count("systemctl stop huangque-content huangque-admin"),
        )
        self.assertEqual(
            2,
            events.count("systemctl restart huangque-content huangque-admin"),
        )
        self.assertEqual(b"old-short-drama-workspace.js\n", live["short-drama-workspace.js"])
        self.assertEqual(b"old-short-drama-workspace.css\n", live["short-drama-workspace.css"])
        self.assertEqual(b"old-short-drama.html\n", live["short-drama.html"])

    def test_formal_delivery_rollback_failure_keeps_services_stopped(self):
        result, events, _curls, _live = self._run_formal_delivery_transition(
            public_mode="fail", rollback_fail=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            2,
            events.count("systemctl stop huangque-content huangque-admin"),
        )
        self.assertEqual(
            1,
            events.count("systemctl restart huangque-content huangque-admin"),
        )
        self.assertIn("services remain stopped", result.stderr)

    def test_signed_provider_file_route_is_public_only_for_valid_current_signature(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "OUT_DIR", Path(tmp)), \
                patch.object(core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"), \
                patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32), \
                patch.object(core, "LOCAL_FILE_URL_TTL", 3600):
            reference = Path(tmp) / "short_drama_role_uploads" / "role.png"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(self.PNG_1X1)
            signed = urllib.parse.urlparse(core.local_provider_reference_url(
                "short_drama_role_uploads/role.png", now=1000
            ))
            server = core.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = "http://127.0.0.1:%d%s" % (server.server_port, signed.path)
            try:
                with patch.object(core.time, "time", return_value=1000):
                    with opener.open(base + "?" + signed.query, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())

                query = urllib.parse.parse_qs(signed.query)
                query["hq_sig"] = ["0" * 64]
                with patch.object(core.time, "time", return_value=1000), \
                        self.assertRaises(urllib.error.HTTPError) as tampered:
                    opener.open(base + "?" + urllib.parse.urlencode(query, doseq=True), timeout=2)
                self.assertEqual(404, tampered.exception.code)

                with patch.object(core.time, "time", return_value=4601), \
                        self.assertRaises(urllib.error.HTTPError) as expired:
                    opener.open(base + "?" + signed.query, timeout=2)
                self.assertEqual(404, expired.exception.code)

                traversal = "http://127.0.0.1:%d/api/gen/file/%%2e%%2e/role.png?%s" % (
                    server.server_port, signed.query,
                )
                with patch.object(core.time, "time", return_value=1000), \
                        self.assertRaises(urllib.error.HTTPError) as escaped:
                    opener.open(traversal, timeout=2)
                self.assertEqual(404, escaped.exception.code)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_output_file_byte_ranges_support_media_streaming(self):
        self.assertIsNone(core._parse_byte_range(None, 100))
        self.assertEqual(core._parse_byte_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(core._parse_byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(core._parse_byte_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            core._parse_byte_range("bytes=100-", 100)
        with self.assertRaises(ValueError):
            core._parse_byte_range("bytes=0-1,4-5", 100)

    def test_download_proxy_sends_douyin_referer(self):
        self.assertIn(".hdslb.com", dl_service.ALLOW)
        self.assertEqual(
            dl_service.download_headers("v26-webf.douyinvod.com")["Referer"],
            "https://www.douyin.com/",
        )
        self.assertNotIn("Referer", dl_service.download_headers("sns-video-hw.xhscdn.com"))

    def test_download_proxy_health_endpoint(self):
        server = dl_service.ThreadingHTTPServer(("127.0.0.1", 0), dl_service.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(
                "http://127.0.0.1:%d/api/gen/dl/health" % server.server_port,
                timeout=2,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"ok": True, "service": "huangque-dl"},
                )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_private_cos_upload_sets_object_acl_and_returns_signed_url(self):
        client = Mock()
        client.get_presigned_url.return_value = "https://signed.example/video"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            url = cos.upload(str(source), "video/private.mp4", "video/mp4", private=True)

        self.assertEqual(url, "https://signed.example/video")
        self.assertEqual(client.put_object.call_args.kwargs["ACL"], "private")

    def test_cos_upload_preserves_prefixed_custom_metadata(self):
        client = Mock()
        client.get_presigned_url.return_value = "https://signed.example/video"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            cos.upload(
                source,
                "video/private.mp4",
                "video/mp4",
                private=True,
                metadata={"X-COS-META-SHA256": "digest"},
            )

        self.assertEqual(
            {"x-cos-meta-sha256": "digest"},
            client.put_object.call_args.kwargs["Metadata"],
        )

    def test_cos_upload_rejects_unprefixed_custom_metadata(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "_client", return_value=client):
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            with self.assertRaisesRegex(ValueError, "x-cos-meta-"):
                cos.upload(
                    source,
                    "video/private.mp4",
                    metadata={"sha256": "digest"},
                )

        client.put_object.assert_not_called()

    def test_sensitive_local_file_requires_matching_asset_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "assets.db")
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.execute("INSERT INTO video_assets VALUES(?,?,?,?,?,?)",
                             ("alice", "done", None, None, "video/tryon_person_a.mp4", "video/tryon_a.mp4"))
                conn.commit()
            with patch.object(core, "AUDIO_DB", db):
                self.assertTrue(core._user_owns_output_file("alice", "video/tryon_a.mp4"))
                self.assertFalse(core._user_owns_output_file("bob", "video/tryon_a.mp4"))
                self.assertTrue(core._sensitive_output_file("video/tryon_a.mp4"))

    def test_playback_bundle_requires_project_or_board_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_db = str(Path(tmp) / "assets.db")
            job_db = str(Path(tmp) / "jobs.db")
            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.execute(
                    "CREATE TABLE short_drama_projects("
                    "id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_composition_versions("
                    "project_id TEXT,file TEXT,cover_file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_playback_versions("
                    "project_id TEXT,media_file TEXT,subtitle_file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_lipsync_versions("
                    "project_id TEXT,file TEXT)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_provider_shot_versions("
                    "project_id TEXT,file TEXT)"
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    ("personal", "alice", None),
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    ("shared", "alice", "board-1"),
                )
                conn.execute(
                    "INSERT INTO short_drama_playback_versions VALUES(?,?,?)",
                    (
                        "personal",
                        "short_drama_playback/personal/bundle/playback.mp4",
                        "short_drama_playback/personal/bundle/subtitles.vtt",
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_lipsync_versions VALUES(?,?)",
                    ("shared", "lipsync/shared/shot-1/job-1.mp4"),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions VALUES(?,?)",
                    ("personal", "video/short-drama-shot-1.mp4"),
                )
                conn.execute(
                    "INSERT INTO short_drama_playback_versions VALUES(?,?,?)",
                    (
                        "shared",
                        "short_drama_playback/shared/bundle/playback.mp4",
                        "short_drama_playback/shared/bundle/subtitles.vtt",
                    ),
                )
                conn.commit()
            with patch.object(core, "AUDIO_DB", audio_db), \
                    patch.object(core, "JOB_DB", job_db):
                self.assertTrue(core._user_owns_output_file(
                    "alice",
                    "short_drama_playback/personal/bundle/playback.mp4",
                ))
                self.assertTrue(core._user_owns_output_file(
                    "alice", "video/short-drama-shot-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob", "video/short-drama-shot-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob",
                    "short_drama_playback/personal/bundle/subtitles.vtt",
                ))
                self.assertTrue(core._user_owns_output_file(
                    "bob",
                    "short_drama_playback/shared/bundle/subtitles.vtt",
                    {"board_id": "board-1", "role": "viewer"},
                ))
                self.assertTrue(core._sensitive_output_file(
                    "lipsync/shared/shot-1/job-1.mp4"
                ))
                self.assertFalse(core._user_owns_output_file(
                    "bob", "lipsync/shared/shot-1/job-1.mp4"
                ))
                self.assertTrue(core._user_owns_output_file(
                    "bob", "lipsync/shared/shot-1/job-1.mp4",
                    {"board_id": "board-1", "role": "viewer"},
                ))

    def test_formal_delivery_requires_project_or_board_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_db = str(Path(tmp) / "assets.db")
            job_db = str(Path(tmp) / "jobs.db")
            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.execute(
                    "CREATE TABLE short_drama_projects("
                    "id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_delivery_versions("
                    "project_id TEXT,url TEXT)"
                )
                conn.executemany(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    (("personal", "alice", None), ("shared", "alice", "board-1")),
                )
                conn.executemany(
                    "INSERT INTO short_drama_delivery_versions VALUES(?,?)",
                    (
                        (
                            "personal",
                            "/api/gen/file/short_drama_delivery/personal/job-1/final-2k.mp4",
                        ),
                        (
                            "shared",
                            "/api/gen/file/short_drama_delivery/shared/job-2/final-2k.mp4",
                        ),
                    ),
                )
                conn.commit()
            personal = "short_drama_delivery/personal/job-1/final-2k.mp4"
            shared = "short_drama_delivery/shared/job-2/final-2k.mp4"
            with patch.object(core, "AUDIO_DB", audio_db), \
                    patch.object(core, "JOB_DB", job_db):
                self.assertTrue(core._sensitive_output_file(personal))
                self.assertTrue(core._user_owns_output_file("alice", personal))
                self.assertFalse(core._user_owns_output_file("bob", personal))
                self.assertFalse(core._user_owns_output_file("bob", shared))
                self.assertTrue(core._user_owns_output_file(
                    "bob", shared, {"board_id": "board-1", "role": "viewer"},
                ))

    def test_formal_delivery_http_route_enforces_auth_and_private_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_db = str(root / "assets.db")
            job_db = str(root / "jobs.db")
            relative = "short_drama_delivery/personal/job-1/final-2k.mp4"
            media = root / relative
            media.parent.mkdir(parents=True)
            media.write_bytes(b"0123456789")
            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.execute(
                    "CREATE TABLE short_drama_projects("
                    "id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_delivery_versions("
                    "project_id TEXT,url TEXT)"
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES('personal','alice',NULL,0)"
                )
                conn.execute(
                    "INSERT INTO short_drama_delivery_versions VALUES(?,?)",
                    ("personal", "/api/gen/file/" + relative),
                )
                conn.commit()

            server = core.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = "http://127.0.0.1:%d/api/gen/file/%s" % (
                server.server_port, relative,
            )
            try:
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value=None), \
                        self.assertRaises(urllib.error.HTTPError) as anonymous:
                    opener.open(url, timeout=2)
                self.assertEqual(401, anonymous.exception.code)

                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "bob"}), \
                        self.assertRaises(urllib.error.HTTPError) as other_user:
                    opener.open(url, timeout=2)
                self.assertEqual(404, other_user.exception.code)

                request = urllib.request.Request(url, headers={"Range": "bytes=2-5"})
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "alice"}):
                    with opener.open(request, timeout=2) as response:
                        self.assertEqual(206, response.status)
                        self.assertEqual(b"2345", response.read())
                        self.assertEqual("bytes 2-5/10", response.headers["Content-Range"])
                        self.assertIn("no-store", response.headers["Cache-Control"])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_formal_delivery_http_route_enforces_canvas_board_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_db = str(root / "assets.db")
            job_db = str(root / "jobs.db")
            relative = "short_drama_delivery/shared/job-1/final-2k.mp4"
            media = root / relative
            media.parent.mkdir(parents=True)
            media.write_bytes(b"shared-delivery")
            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.execute(
                    "CREATE TABLE short_drama_projects("
                    "id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE short_drama_delivery_versions(project_id TEXT,url TEXT)"
                )
                conn.execute(
                    "INSERT INTO short_drama_projects VALUES('shared','alice','board-1',0)"
                )
                conn.execute(
                    "INSERT INTO short_drama_delivery_versions VALUES(?,?)",
                    ("shared", "/api/gen/file/" + relative),
                )
                conn.commit()

            server = core.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = "http://127.0.0.1:%d/api/gen/file/%s" % (
                server.server_port, relative,
            )

            def access(handler):
                board_id = handler.headers.get("X-Canvas-Board-Id")
                role = handler.headers.get("X-Test-Board-Role") or "viewer"
                return {"board_id": board_id, "role": role} if board_id else None

            try:
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "bob"}), \
                        patch.object(core, "_short_drama_canvas_access", side_effect=access):
                    wrong_board = urllib.request.Request(url, headers={
                        "Authorization": "Bearer test",
                        "X-Canvas-Board-Id": "board-2",
                    })
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        opener.open(wrong_board, timeout=2)
                    self.assertEqual(404, denied.exception.code)

                    for role in ("viewer", "editor"):
                        allowed = urllib.request.Request(url, headers={
                            "Authorization": "Bearer test",
                            "X-Canvas-Board-Id": "board-1",
                            "X-Test-Board-Role": role,
                        })
                        with opener.open(allowed, timeout=2) as response:
                            self.assertEqual(200, response.status)
                            self.assertEqual(b"shared-delivery", response.read())
                            self.assertIn("no-store", response.headers["Cache-Control"])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_scene_upload_http_route_enforces_graph_project_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_db = str(root / "assets.db")
            job_db = str(root / "jobs.db")
            owner_hash = hashlib.sha256(b"alice").hexdigest()[:16]
            personal_hash = hashlib.sha256(b"personal").hexdigest()[:16]
            shared_hash = hashlib.sha256(b"shared").hexdigest()[:16]
            personal = (
                "short_drama_scene_uploads/%s/%s/scene_private.png"
                % (owner_hash, personal_hash)
            )
            shared = (
                "short_drama_scene_uploads/%s/%s/scene_shared.png"
                % (owner_hash, shared_hash)
            )
            forged = (
                "short_drama_scene_uploads/%s/%s/scene_without_audit.png"
                % (owner_hash, personal_hash)
            )
            cross_project = (
                "short_drama_scene_uploads/%s/%s/scene_cross_project.png"
                % (owner_hash, personal_hash)
            )
            wrong_prefix = "short_drama_scene_uploads/wrong/wrong/scene_wrong.png"
            for relative, content in (
                (personal, self.PNG_1X1),
                (shared, self.PNG_1X1),
                (forged, self.PNG_1X1),
                (cross_project, self.PNG_1X1),
                (wrong_prefix, self.PNG_1X1),
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            with closing(sqlite3.connect(audio_db)) as conn:
                conn.execute("CREATE TABLE video_assets(username TEXT,status TEXT,image_file TEXT,audio_file TEXT,reference_video_file TEXT,video_file TEXT)")
                conn.execute("CREATE TABLE avatars(username TEXT,status TEXT,image_file TEXT)")
                conn.execute("CREATE TABLE audio_voices(username TEXT,scope TEXT,preview_file TEXT)")
                conn.execute(
                    "INSERT INTO video_assets VALUES(?,?,?,?,?,?)",
                    ("bob", "done", personal, None, None, None),
                )
                conn.commit()
            with closing(sqlite3.connect(job_db)) as conn:
                conn.executescript("""
                    CREATE TABLE short_drama_projects(
                      id TEXT PRIMARY KEY,username TEXT,board_id TEXT,deleted INTEGER
                    );
                    CREATE TABLE short_drama_graph_entities(
                      id TEXT PRIMARY KEY,project_id TEXT,asset_type TEXT,status TEXT
                    );
                    CREATE TABLE short_drama_graph_versions(
                      entity_id TEXT,references_json TEXT,attributes_json TEXT
                    );
                    CREATE TABLE short_drama_graph_audit(
                      id INTEGER PRIMARY KEY,project_id TEXT,actor TEXT,action TEXT,
                      target_id TEXT,details_json TEXT,created_at INTEGER
                    );
                """)
                conn.executemany(
                    "INSERT INTO short_drama_projects VALUES(?,?,?,0)",
                    (
                        ("personal", "alice", None),
                        ("shared", "alice", "board-1"),
                        ("bob-project", "bob", None),
                        ("other", "alice", None),
                    ),
                )
                conn.executemany(
                    "INSERT INTO short_drama_graph_entities VALUES(?,?,'scene','active')",
                    (
                        ("scene-personal", "personal"),
                        ("scene-shared", "shared"),
                        ("scene-forged", "personal"),
                        ("scene-wrong", "personal"),
                        ("scene-bob-forged", "bob-project"),
                        ("scene-cross-project", "other"),
                    ),
                )
                conn.executemany(
                    "INSERT INTO short_drama_graph_versions VALUES(?,?,?)",
                    (
                        (
                            "scene-personal",
                            json.dumps([{"file": personal, "url": "/api/gen/file/" + personal}]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-personal",
                                "scene_reference_owner": "alice",
                                "scene_reference_actor": "alice",
                                "scene_reference_project_id": "personal",
                            }),
                        ),
                        (
                            "scene-shared",
                            json.dumps([{"file": shared, "url": "/api/gen/file/" + shared}]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-shared",
                                "scene_reference_owner": "alice",
                                "scene_reference_actor": "alice",
                                "scene_reference_project_id": "shared",
                            }),
                        ),
                        (
                            "scene-forged",
                            json.dumps([{"file": forged, "url": "/api/gen/file/" + forged}]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-missing",
                                "scene_reference_owner": "alice",
                                "scene_reference_actor": "alice",
                                "scene_reference_project_id": "personal",
                            }),
                        ),
                        (
                            "scene-wrong",
                            json.dumps([{
                                "file": wrong_prefix,
                                "url": "/api/gen/file/" + wrong_prefix,
                            }]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-wrong-prefix",
                                "scene_reference_owner": "alice",
                                "scene_reference_actor": "alice",
                                "scene_reference_project_id": "personal",
                            }),
                        ),
                        (
                            "scene-bob-forged",
                            json.dumps([{
                                "file": personal,
                                "url": "/api/gen/file/" + personal,
                            }]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-bob-forged",
                                "scene_reference_owner": "bob",
                                "scene_reference_actor": "bob",
                                "scene_reference_project_id": "bob-project",
                            }),
                        ),
                        (
                            "scene-cross-project",
                            json.dumps([{
                                "file": cross_project,
                                "url": "/api/gen/file/" + cross_project,
                            }]),
                            json.dumps({
                                "source": "upload",
                                "scene_operation_id": "operation-cross-project",
                                "scene_reference_owner": "alice",
                                "scene_reference_actor": "alice",
                                "scene_reference_project_id": "other",
                            }),
                        ),
                    ),
                )
                conn.executemany(
                    "INSERT INTO short_drama_graph_audit"
                    "(project_id,actor,action,target_id,details_json,created_at) "
                    "VALUES(?,?,'set_scene_reference',?,?,1)",
                    (
                        (
                            "personal", "alice", "scene-group:personal",
                            json.dumps({
                                "source": "upload",
                                "operation_id": "operation-personal",
                            }),
                        ),
                        (
                            "shared", "alice", "scene-group:shared",
                            json.dumps({
                                "source": "upload",
                                "operation_id": "operation-shared",
                            }),
                        ),
                        (
                            "personal", "alice", "scene-group:wrong-prefix",
                            json.dumps({
                                "source": "upload",
                                "operation_id": "operation-wrong-prefix",
                            }),
                        ),
                        (
                            "bob-project", "bob", "scene-group:bob-forged",
                            json.dumps({
                                "source": "upload",
                                "operation_id": "operation-bob-forged",
                            }),
                        ),
                        (
                            "other", "alice", "scene-group:cross-project",
                            json.dumps({
                                "source": "upload",
                                "operation_id": "operation-cross-project",
                            }),
                        ),
                    ),
                )
                conn.commit()

            server = core.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = "http://127.0.0.1:%d/api/gen/file/" % server.server_port

            def access(handler):
                board_id = handler.headers.get("X-Canvas-Board-Id")
                return {"board_id": board_id, "role": "viewer"} if board_id else None

            try:
                common = (
                    patch.object(core, "OUT_DIR", root),
                    patch.object(core, "AUDIO_DB", audio_db),
                    patch.object(core, "JOB_DB", job_db),
                )
                with common[0], common[1], common[2], \
                        patch.object(core, "verify", return_value=None), \
                        self.assertRaises(urllib.error.HTTPError) as anonymous:
                    opener.open(base + personal, timeout=2)
                self.assertEqual(401, anonymous.exception.code)

                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "bob"}), \
                        self.assertRaises(urllib.error.HTTPError) as other_user:
                    opener.open(base + personal, timeout=2)
                self.assertEqual(404, other_user.exception.code)

                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "alice"}):
                    with opener.open(base + personal, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())

                with closing(sqlite3.connect(job_db)) as conn:
                    conn.execute(
                        "UPDATE short_drama_graph_entities SET status='retired' WHERE id=?",
                        ("scene-personal",),
                    )
                    conn.commit()
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "alice"}), \
                        self.assertRaises(urllib.error.HTTPError) as retired:
                    opener.open(base + personal, timeout=2)
                self.assertEqual(404, retired.exception.code)

                with closing(sqlite3.connect(job_db)) as conn:
                    conn.execute(
                        "UPDATE short_drama_graph_entities SET status='active' WHERE id=?",
                        ("scene-personal",),
                    )
                    conn.commit()
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "alice"}):
                    with opener.open(base + personal, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())

                for untrusted in (forged, wrong_prefix, cross_project):
                    with patch.object(core, "OUT_DIR", root), \
                            patch.object(core, "AUDIO_DB", audio_db), \
                            patch.object(core, "JOB_DB", job_db), \
                            patch.object(core, "verify", return_value={"username": "alice"}), \
                            self.assertRaises(urllib.error.HTTPError) as denied:
                        opener.open(base + untrusted, timeout=2)
                    self.assertEqual(404, denied.exception.code)

                board_request = urllib.request.Request(
                    base + shared,
                    headers={"X-Canvas-Board-Id": "board-1"},
                )
                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "verify", return_value={"username": "bob"}), \
                        patch.object(core, "_short_drama_canvas_access", side_effect=access):
                    with opener.open(board_request, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())

                with patch.object(core, "OUT_DIR", root), \
                        patch.object(core, "AUDIO_DB", audio_db), \
                        patch.object(core, "JOB_DB", job_db), \
                        patch.object(core, "LOCAL_FILE_PUBLIC_BASE_URL", "https://media.example"), \
                        patch.object(core, "LOCAL_FILE_SIGNING_SECRET", "s" * 32), \
                        patch.object(core.time, "time", return_value=1000), \
                        patch.object(core, "verify", return_value=None):
                    signed = urllib.parse.urlparse(
                        core.local_provider_reference_url(personal, now=1000)
                    )
                    signed_url = (
                        "http://127.0.0.1:%d%s?%s"
                        % (server.server_port, signed.path, signed.query)
                    )
                    with opener.open(signed_url, timeout=2) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(self.PNG_1X1, response.read())
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_download_proxy_token_verification_fails_closed(self):
        response = Mock()
        response.read.return_value = json.dumps({"user": {"username": "alice"}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(dl_service.urllib.request, "urlopen", return_value=response):
            self.assertTrue(dl_service.verify_token("valid"))
        with patch.object(dl_service.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(dl_service.verify_token("valid"))

    def test_download_proxy_accepts_only_matching_internal_token(self):
        original = dl_service.INTERNAL_TOKEN
        try:
            dl_service.INTERNAL_TOKEN = "internal-test-token"
            self.assertTrue(dl_service.verify_internal({
                "X-HQ-Internal-Token": "internal-test-token",
            }))
            self.assertFalse(dl_service.verify_internal({
                "X-HQ-Internal-Token": "wrong",
            }))
        finally:
            dl_service.INTERNAL_TOKEN = original


if __name__ == "__main__":
    unittest.main()
