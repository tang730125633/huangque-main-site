import base64
import hashlib
import importlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBgYgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x04test"


class DigitalHumanLocalMaterialLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = str(pathlib.Path(__file__).resolve().parents[1] / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.domain = importlib.import_module("content_domains.digital_human_v2")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "huangque-media"
        (self.root / "files" / "图片").mkdir(parents=True)
        (self.root / "files" / "视频").mkdir(parents=True)
        (self.root / "files" / "BGM").mkdir(parents=True)
        self.records = []
        self.environment = mock.patch.dict(os.environ, {
            self.domain._LOCAL_LIBRARY_ENV: str(self.root),
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def add(self, relative, media_type, mime, content, **updates):
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        record = {
            "状态": "可使用", "素材类型": media_type,
            "server_relative_path": relative,
            "SHA256": hashlib.sha256(content).hexdigest(), "MIME": mime,
            "素材名称": path.stem, "标签": ["本地", "安全"],
        }
        record.update(updates)
        self.records.append(record)
        return path, record

    def write_index(self, records=None):
        records = self.records if records is None else records
        (self.root / "index.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )

    def valid_three(self):
        self.add("files/图片/a.png", "图片", "image/png", PNG)
        self.add("files/视频/a.mp4", "视频", "video/mp4", MP4)
        self.add("files/BGM/a.mp3", "BGM", "audio/mpeg", MP3)
        self.write_index()

    def test_success_has_three_types_and_only_opaque_internal_ids(self):
        self.valid_three()
        result = self.domain.local_material_library_operational_probe(expected_count=3)
        self.assertEqual({"image": 1, "video": 1, "bgm": 1}, result["types"])
        _, records = self.domain._load_local_catalog(expected_count=3)
        self.assertTrue(all(item["id"].startswith("local_") for item in records))
        public = {"source": "local_library", "material_asset_id": "dhm_" + "a" * 32}
        self.assertNotIn("server_relative_path", public)
        self.assertNotIn("source_url", public)
        self.assertNotIn("file_token", public)

    def test_operational_probe_defaults_to_production_contract(self):
        records = [
            {"media_type": "image"},
            {"media_type": "video"},
            {"media_type": "bgm"},
        ]
        with mock.patch.object(
                self.domain, "_load_local_catalog",
                return_value=(self.root, records)) as load:
            with mock.patch.object(self.domain, "_read_local_record"):
                result = self.domain.local_material_library_operational_probe()
        load.assert_called_once_with(expected_count=318)
        self.assertEqual(3, result["count"])

    def test_release_probe_can_verify_every_indexed_file(self):
        self.valid_three()
        with mock.patch.object(
            self.domain, "_read_local_record",
            wraps=self.domain._read_local_record,
        ) as read:
            result = self.domain.local_material_library_operational_probe(
                expected_count=3, verify_all=True,
            )
        self.assertEqual(3, result["verified_files"])
        self.assertEqual(3, read.call_count)

    def test_missing_directory_and_root_symlink_fail_closed(self):
        missing = self.root.parent / "missing"
        with mock.patch.dict(os.environ, {self.domain._LOCAL_LIBRARY_ENV: str(missing)}):
            with self.assertRaises((OSError, ValueError)):
                self.domain._load_local_catalog()
        if os.name == "posix":
            linked = self.root.parent / "linked"
            linked.symlink_to(self.root, target_is_directory=True)
            with mock.patch.dict(os.environ, {self.domain._LOCAL_LIBRARY_ENV: str(linked)}):
                with self.assertRaisesRegex(ValueError, "软链接"):
                    self.domain._load_local_catalog()

    def test_traversal_absolute_hidden_and_duplicate_paths_are_rejected(self):
        self.add("files/图片/a.png", "图片", "image/png", PNG)
        base = dict(self.records[0])
        for bad in ("../escape.png", "/tmp/escape.png", "files/.incoming/a.png"):
            with self.subTest(path=bad):
                record = dict(base, server_relative_path=bad)
                self.write_index([record])
                with self.assertRaises(ValueError):
                    self.domain._load_local_catalog()
        self.write_index([base, dict(base)])
        with self.assertRaisesRegex(ValueError, "重复"):
            self.domain._load_local_catalog()

    def test_index_line_count_line_size_and_total_size_are_bounded(self):
        self.add("files/图片/a.png", "图片", "image/png", PNG)
        with mock.patch.object(self.domain, "_LOCAL_INDEX_MAX_LINES", 1):
            self.write_index([self.records[0], self.records[0]])
            with self.assertRaises(ValueError):
                self.domain._load_local_catalog()
        with mock.patch.object(self.domain, "_LOCAL_INDEX_MAX_LINE_BYTES", 32):
            self.write_index()
            with self.assertRaisesRegex(ValueError, "单行"):
                self.domain._load_local_catalog()
        with mock.patch.object(self.domain, "_LOCAL_INDEX_MAX_BYTES", 32):
            self.write_index()
            with self.assertRaises(ValueError):
                self.domain._load_local_catalog()

    def test_file_symlink_and_non_regular_file_are_rejected(self):
        if os.name != "posix":
            self.skipTest("real symlink and FIFO checks require POSIX")
        target = self.root / "target.png"
        target.write_bytes(PNG)
        link = self.root / "files/图片/link.png"
        link.symlink_to(target)
        self.records = [{
            "状态": "可使用", "素材类型": "图片", "MIME": "image/png",
            "server_relative_path": "files/图片/link.png",
            "SHA256": hashlib.sha256(PNG).hexdigest(),
        }]
        self.write_index()
        with self.assertRaises(ValueError):
            self.domain._load_local_catalog()
        link.unlink()
        os.mkfifo(link)
        with self.assertRaises(ValueError):
            self.domain._load_local_catalog()

    def test_hash_mismatch_cross_class_and_unknown_magic_fail_closed(self):
        _, record = self.add("files/图片/a.png", "图片", "image/png", PNG)
        self.write_index()
        root, records = self.domain._load_local_catalog()
        record["SHA256"] = "f" * 64
        self.write_index([record])
        _, mismatched = self.domain._load_local_catalog()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.domain._read_local_record(root, mismatched[0])
        (self.root / "files/图片/a.png").write_bytes(MP4)
        record["SHA256"] = hashlib.sha256(MP4).hexdigest()
        self.write_index([record])
        _, spoofed = self.domain._load_local_catalog()
        with self.assertRaisesRegex(ValueError, "素材大类"):
            self.domain._read_local_record(root, spoofed[0])
        unknown = b"unknown-media-magic"
        (self.root / "files/图片/a.png").write_bytes(unknown)
        record["SHA256"] = hashlib.sha256(unknown).hexdigest()
        self.write_index([record])
        _, unknown_record = self.domain._load_local_catalog()
        with self.assertRaisesRegex(ValueError, "素材大类"):
            self.domain._read_local_record(root, unknown_record[0])

    def test_same_class_mislabeled_jpeg_index_with_png_magic_uses_real_mime(self):
        self.add("files/图片/poster.jpg", "图片", "image/jpeg", PNG)
        self.write_index()
        root, records = self.domain._load_local_catalog()
        raw, actual_mime = self.domain._read_local_record(root, records[0])
        self.assertEqual(PNG, raw)
        self.assertEqual("image/png", actual_mime)
        with mock.patch.object(
                self.domain, "_load_local_catalog", return_value=(root, records)):
            selected = self.domain._local_library_material("海报", "image")
        self.assertEqual("image/png", selected[1])
        self.assertEqual("local_library", selected[2])

    def test_replacement_after_open_or_read_fails_closed(self):
        path, _ = self.add("files/图片/a.png", "图片", "image/png", PNG)
        self.write_index()
        root, records = self.domain._load_local_catalog()
        original_lstat = os.lstat

        def replaced_lstat(value):
            if os.path.abspath(value) == os.path.abspath(path):
                path.write_bytes(PNG + b"replacement")
            return original_lstat(value)

        with mock.patch.object(self.domain.os, "lstat", side_effect=replaced_lstat):
            with self.assertRaises(ValueError):
                self.domain._read_local_record(root, records[0])

    def test_unreadable_material_fails_for_service_user(self):
        if os.name != "posix":
            self.skipTest("POSIX permission check")
        path, _ = self.add("files/图片/a.png", "图片", "image/png", PNG)
        self.write_index()
        root, records = self.domain._load_local_catalog()
        path.chmod(0)
        try:
            with self.assertRaises((OSError, ValueError)):
                self.domain._read_local_record(root, records[0])
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_request_cannot_supply_server_path(self):
        for field in ("server_relative_path", "material_root", "source_url"):
            with self.subTest(field=field), self.assertRaisesRegex(
                    self.domain.DigitalHumanRequestError, "不支持字段"):
                self.domain.resolve_material_response({field: "/tmp/escape"}, "user")


if __name__ == "__main__":
    unittest.main()
