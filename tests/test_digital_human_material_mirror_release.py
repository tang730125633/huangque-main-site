import base64
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/digital_human_material_mirror_release.py"
sys.path.insert(0, str(ROOT / "server"))
spec = importlib.util.spec_from_file_location(
    "digital_human_material_mirror_release", SCRIPT,
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBgYgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x04test"


class DigitalHumanMaterialMirrorReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.output_a = self.base / "releases-a"
        self.output_b = self.base / "releases-b"
        self.target = self.base / "production-mirror"
        self.source.mkdir()
        self._write_source()
        self.contract = mock.patch.multiple(
            release.preflight,
            EXPECTED_LIBRARY_PRIMARY_HOST="8.148.158.106",
            EXPECTED_LIBRARY_PRIMARY_ROOT=str(self.source),
            EXPECTED_LIBRARY_ROOT=str(self.target),
            EXPECTED_LIBRARY_COUNT=3,
        )
        self.contract.start()

    def tearDown(self):
        self.contract.stop()
        self.temporary.cleanup()

    def _write_source(self):
        assets = (
            ("files/图片/a.png", "图片", "image/png", PNG),
            ("files/视频/a.mp4", "视频", "video/mp4", MP4),
            ("files/BGM/a.mp3", "BGM", "audio/mpeg", MP3),
        )
        records = []
        for relative, material_type, mime, raw in assets:
            path = self.source.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            records.append({
                "状态": "可使用",
                "素材类型": material_type,
                "server_relative_path": relative,
                "SHA256": hashlib.sha256(raw).hexdigest(),
                "MIME": mime,
                "素材名称": path.stem,
                "标签": ["已审核"],
            })
        (self.source / "index.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n" for item in records
            ),
            encoding="utf-8",
        )

    def _build(self, output=None):
        output = output or self.output_a
        result = release.build_release(
            self.source,
            output,
            source_host="8.148.158.106",
            source_root_contract=self.source,
            mirror_root=self.target,
            expected_count=3,
        )
        return pathlib.Path(result["release_directory"]), result

    def _install(self, directory, **updates):
        arguments = {
            "runtime_probe": lambda _target: None,
            "expected_source_host": "8.148.158.106",
            "expected_source_root": self.source,
            "expected_mirror_root": self.target,
            "expected_count": 3,
        }
        arguments.update(updates)
        return release.install_release(directory, self.target, **arguments)

    def test_build_is_immutable_and_deterministic(self):
        first_directory, first = self._build(self.output_a)
        second_directory, second = self._build(self.output_b)
        self.assertEqual(first["release_id"], first_directory.name)
        self.assertEqual(first["bundle_sha256"], second["bundle_sha256"])
        self.assertEqual(first["index_sha256"], second["index_sha256"])
        manifest, _bundle, _bundle_bytes = release._load_release(
            first_directory,
            expected_source_root=self.source,
            expected_mirror_root=self.target,
            expected_count=3,
        )
        self.assertEqual(5, manifest["file_count"])
        with self.assertRaisesRegex(release.MirrorReleaseError, "approved value"):
            release._load_release(
                first_directory,
                expected_source_root=self.source,
                expected_mirror_root=self.target,
                expected_count=3,
                expected_manifest_sha256="0" * 64,
            )
        with self.assertRaisesRegex(release.MirrorReleaseError, "already exists"):
            self._build(self.output_a)

    def test_build_consumes_material_payloads_one_at_a_time(self):
        domain = release._digital_human_domain()
        original_read = domain._read_local_record
        original_write = release._write_new
        original_verify = release.verify_snapshot
        pending = []
        reads = []
        tracking = {"active": True}

        def tracked_read(root, record):
            if not tracking["active"]:
                return original_read(root, record)
            self.assertFalse(
                pending, "next material was read before the prior payload was written",
            )
            raw, mime = original_read(root, record)
            pending.append(record["relative"])
            reads.append(record["relative"])
            return raw, mime

        def tracked_write(path, raw, mode=0o600):
            result = original_write(path, raw, mode)
            if "/snapshot/files/" in pathlib.Path(path).as_posix():
                self.assertEqual(1, len(pending))
                pending.clear()
            return result

        def stop_tracking_before_verify(*args, **kwargs):
            tracking["active"] = False
            return original_verify(*args, **kwargs)

        with mock.patch.object(
                domain, "_read_local_record", side_effect=tracked_read):
            with mock.patch.object(
                    release, "_write_new", side_effect=tracked_write):
                with mock.patch.object(
                        release, "verify_snapshot",
                        side_effect=stop_tracking_before_verify):
                    directory, manifest = self._build()

        self.assertFalse(pending)
        self.assertEqual(3, len(reads))
        self.assertEqual(5, manifest["file_count"])
        self.assertTrue(directory.exists())

    def test_tampered_bundle_is_rejected_before_target_changes(self):
        directory, _manifest = self._build()
        self.target.mkdir()
        (self.target / "old.txt").write_text("old", encoding="utf-8")
        bundle = directory / release.RELEASE_BUNDLE_NAME
        if os.name == "posix":
            os.chmod(bundle, 0o600)
        with bundle.open("ab") as output:
            output.write(b"tampered")
        with self.assertRaisesRegex(release.MirrorReleaseError, "digest"):
            self._install(directory)
        self.assertEqual("old", (self.target / "old.txt").read_text("utf-8"))

    def test_bundle_rejects_path_traversal_and_symlink(self):
        for member_type, name in ((tarfile.REGTYPE, "../escape"),
                                  (tarfile.SYMTYPE, "files/图片/link")):
            with self.subTest(member_type=member_type):
                archive_path = self.base / ("unsafe-%s.tar" % member_type)
                with tarfile.open(archive_path, "w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = member_type
                    if member_type == tarfile.REGTYPE:
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b"x"))
                    else:
                        info.linkname = "/tmp/escape"
                        archive.addfile(info)
                destination = self.base / ("extract-%s" % member_type)
                with self.assertRaises(release.MirrorReleaseError):
                    release._extract_bundle(archive_path, destination)

    def test_successful_install_keeps_prior_snapshot_as_backup(self):
        directory, manifest = self._build()
        self.target.mkdir()
        (self.target / "old.txt").write_text("old", encoding="utf-8")
        result = self._install(directory)
        self.assertTrue(result["ok"])
        self.assertEqual(manifest["index_sha256"], result["index_sha256"])
        backup = pathlib.Path(result["backup"])
        self.assertEqual("old", (backup / "old.txt").read_text("utf-8"))
        verified = release.verify_snapshot(self.target, expected_count=3)
        self.assertEqual(manifest["index_sha256"], verified["index_sha256"])

    def test_each_switch_failure_restores_prior_snapshot(self):
        for phase in ("prepared", "old_moved", "new_active", "runtime_preflight"):
            with self.subTest(phase=phase):
                if self.target.exists():
                    import shutil
                    shutil.rmtree(self.target)
                self.target.mkdir()
                (self.target / "old.txt").write_text("old", encoding="utf-8")
                directory, _manifest = self._build(
                    self.base / ("releases-" + phase)
                )
                with self.assertRaises(release.InjectedMirrorFailure):
                    self._install(directory, fault_after=phase)
                self.assertEqual(
                    "old", (self.target / "old.txt").read_text("utf-8"),
                )
                journal = self.target.parent / (
                    ".%s.transaction.json" % self.target.name
                )
                self.assertFalse(journal.exists())

    def test_recover_restores_backup_after_process_interruption(self):
        directory, manifest = self._build()
        self.target.mkdir()
        (self.target / "old.txt").write_text("old", encoding="utf-8")
        paths = release._transaction_paths(self.target, manifest["release_id"])
        paths["staging"].mkdir()
        os.replace(str(self.target), str(paths["backup"]))
        state = {
            "schema_version": release.TRANSACTION_SCHEMA_VERSION,
            "phase": "old_moved",
            "target": str(self.target),
            "staging": str(paths["staging"]),
            "backup": str(paths["backup"]),
            "failed": str(paths["failed"]),
            "release_id": manifest["release_id"],
        }
        release._write_journal(paths["journal"], state)
        recovered = release.recover_incomplete(self.target)
        self.assertTrue(recovered["recovered"])
        self.assertEqual("old", (self.target / "old.txt").read_text("utf-8"))
        self.assertFalse(paths["journal"].exists())

    def test_extract_failure_is_moved_to_failure_evidence(self):
        directory, _manifest = self._build()
        original = release.verify_snapshot

        def fail_staging(path, expected_count=None):
            if ".staging." in pathlib.Path(path).name:
                raise release.MirrorReleaseError("injected staged verification failure")
            return original(path, expected_count=expected_count)

        with mock.patch.object(release, "verify_snapshot", side_effect=fail_staging):
            with self.assertRaisesRegex(
                    release.MirrorReleaseError, "staged verification"):
                self._install(directory)
        self.assertFalse(self.target.exists())
        self.assertFalse(any(
            item.name.startswith(".%s.staging." % self.target.name)
            for item in self.target.parent.iterdir()
        ))
        self.assertTrue(any(
            item.name.startswith(".%s.failed." % self.target.name)
            for item in self.target.parent.iterdir()
        ))


if __name__ == "__main__":
    unittest.main()
