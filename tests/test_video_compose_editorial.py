"""Offline template/HTTP-boundary regressions; no production or provider calls."""
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import editorial_contract as contract
from content_domains import editorial_markup
from content_domains import editorial_process_supervisor as supervisor
from content_domains import video_compose as api
from content_domains import video_compose_analysis as analysis
from content_domains import video_compose_asr as asr
from content_domains import video_compose_editorial as editorial
from content_domains import video_compose_render as renderer
from content_domains import video_compose_store as store
import hq_cli_api
from scripts import check_editorial_process_cleanup as process_cleanup_check


def example_plan():
    return {"schema": contract.SCHEMA_ID, "timebase": "edited_output",
        "transcript_hash": "a" * 64, "edit_decision_version": 1,
        "title": ["一个人的效率", "让内容更有价值"],
        "captions": [{"start": .5, "end": 2, "text": "一个人", "en": "One person"},
                     {"start": 2.1, "end": 4, "text": "一台电脑", "en": "One computer"}],
        "keywords": ["一个人"], "camera": [{"at": 0, "scale": 1, "transition": "cut"}],
        "keyword_punches": [{"at": .5, "word": "一个人", "strength": 1.075}], "callouts": []}


def words():
    return [{"text": "一个人", "start_ms": 500, "end_ms": 2000, "timing_source": "provider_word"},
            {"text": "一台电脑", "start_ms": 2100, "end_ms": 4000, "timing_source": "provider_word"}]


def project():
    return {"transcript_hash": "a" * 64, "edit_decision_version": 1, "words": words(),
        "edl": {"keep_ranges": [{"source_start_ms": 0, "source_end_ms": 5000}],
                "output_duration_ms": 5000}}


def body(plan=None, revision=3):
    return {"expected_revision": revision, "template_id": contract.TEMPLATE_ID,
            "editorial_plan": example_plan() if plan is None else plan}


class ContractTests(unittest.TestCase):
    def test_empty_decisions_are_forwarded_for_zero_candidate_projects(self):
        self.assertEqual({}, hq_cli_api._video_compose_decisions({}))
        self.assertEqual({}, analysis.normalize_decisions([], {}))

    def test_valid_plan_is_detached_and_original_style_is_preserved(self):
        plan = example_plan()
        result = contract.validate_plan(plan, 5)
        self.assertEqual(plan, result)
        self.assertIsNot(plan["captions"], result["captions"])
        html = editorial_markup.make_html({**plan, "duration": 5})
        for token in ("SourceHanSerifSC-Heavy.otf", "NotoSansSC-Regular.otf", "One person",
                      'duration:0.18,ease:"power2.inOut"', 'duration:0.24,ease:"power2.inOut"',
                      "scale:1.075", "font-size:74px", "#8b2025"):
            self.assertIn(token, html)
        self.assertEqual(1, html.count("<audio "))
        self.assertEqual(2, html.count("muted playsinline"))
        self.assertIn("background:#181311cc", html)
        self.assertIn(".caption.lower .english{margin-left:auto}", html)

    def test_text_is_escaped_and_cannot_execute(self):
        plan = example_plan()
        plan["title"][0] = "<script>"
        plan["captions"][0]["en"] = '<img src=x onerror="alert(1)">'
        html = editorial_markup.make_html({**contract.validate_plan(plan, 5), "duration": 5})
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn('<img src=x', html)

    def test_rejects_unknown_missing_nested_and_executable_fields(self):
        cases = [None, [], {}, {**example_plan(), "source": "/etc/passwd"}]
        for path, key, value in [("captions", "js", "alert(1)"), ("camera", "scale", True),
                                 ("keyword_punches", "at", float("nan")),
                                 ("camera", "scale", 10**400),
                                 ("captions", "text", "\ud800")]:
            plan = example_plan(); plan[path][0][key] = value; cases.append(plan)
        plan = example_plan(); del plan["captions"][0]["en"]; cases.append(plan)
        plan = example_plan(); plan["camera"] = [None]; cases.append(plan)
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                contract.validate_plan(plan, 5)

    def test_rejects_missing_or_fake_english(self):
        for value in ("", "中文翻译", "12345", "English\nline", "x" * 59):
            plan = example_plan(); plan["captions"][0]["en"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.validate_plan(plan, 5)

    def test_rejects_time_ranges_overlap_and_oversized_text(self):
        cases = []
        for key, value in [("start", -1), ("end", 181), ("end", .55), ("text", "太长" * 10)]:
            plan = example_plan(); plan["captions"][0][key] = value; cases.append(plan)
        plan = example_plan(); plan["captions"][1]["start"] = 1.9; cases.append(plan)
        plan = example_plan(); plan["title"][0] = "标题" * 8; cases.append(plan)
        plan = example_plan(); plan["keywords"] = ["没有说过"]; cases.append(plan)
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                contract.validate_plan(plan, 5)

    def test_rejects_unbounded_camera_and_punch_collisions(self):
        for change in (lambda p: p["camera"][0].update(at=1),
                       lambda p: p["camera"][0].update(scale=1.25),
                       lambda p: p["camera"].append({"at": 1, "scale": 1.1, "transition": "whip"}),
                       lambda p: p["keyword_punches"][0].update(strength=1.2),
                       lambda p: p["keyword_punches"][0].update(at=4.5),
                       lambda p: p["keyword_punches"].append(dict(p["keyword_punches"][0]))):
            plan = example_plan(); change(plan)
            with self.assertRaises(ValueError):
                contract.validate_plan(plan, 5)
        plan = example_plan()
        plan["camera"].append({"at": 6, "scale": 1.1, "transition": "whip"})
        plan["captions"].append({"start": 5.9, "end": 7, "text": "一个人", "en": "One person"})
        plan["keyword_punches"] = [{"at": 5.9, "word": "一个人", "strength": 1.075}]
        with self.assertRaisesRegex(ValueError, "重叠"):
            contract.validate_plan(plan, 8)

    def test_template_selection_never_silently_falls_back(self):
        for value in ({"template_id": "unknown"}, {"template_id": None},
                      {"template_id": contract.TEMPLATE_ID}, {"editorial_plan": example_plan()},
                      {"template_id": contract.LEGACY_TEMPLATES[0], "editorial_plan": example_plan()}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.validate_selection(value)
        self.assertIsNone(contract.validate_selection({}))
        self.assertEqual(example_plan(), contract.validate_selection(body()))

    def test_client_contract_is_exactly_the_server_contract(self):
        self.assertEqual((ROOT / "server/content_domains/editorial_contract.py").read_bytes(),
                         (ROOT / "tools/hq-cli/src/hq_cli/editorial_contract.py").read_bytes())

    def test_transcript_and_edl_bindings_reject_stale_plans(self):
        for key, value in (("transcript_hash", "b" * 64), ("edit_decision_version", 2),
                           ("timebase", "source")):
            plan = example_plan(); plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                editorial.project_input(project(), body(plan))

    def test_punch_is_remapped_after_cut_and_requires_word_boundary(self):
        current = project()
        current["edl"] = {"keep_ranges": [{"source_start_ms": 250, "source_end_ms": 5000}],
                          "output_duration_ms": 4750}
        plan = example_plan()
        for cue in plan["captions"]:
            cue["start"] -= .25; cue["end"] -= .25
        plan["keyword_punches"][0]["at"] = .25
        self.assertEqual(4750, editorial.project_input(current, body(plan))["duration_ms"])
        plan["keyword_punches"][0]["at"] = .6
        with self.assertRaisesRegex(ValueError, "逐词"):
            editorial.project_input(current, body(plan))

    def test_interpolated_or_unknown_timing_cannot_claim_precise_punch(self):
        for source in (None, "segment_interpolated"):
            current = project(); current["words"][0]["timing_source"] = source
            with self.assertRaisesRegex(ValueError, "逐词"):
                editorial.project_input(current, body())

    def test_asr_provenance_survives_normalization(self):
        for payload, expected in (({"words": [{"word": "你好", "start": 0, "end": 1}]}, "provider_word"),
                                  ({"segments": [{"text": "你好", "start": 0, "end": 1}]}, "segment_interpolated")):
            transcript = asr.parse_verbose_response(payload)
            _, normalized = analysis.normalize_words(transcript["words"], 2000)
            self.assertTrue(all(word["timing_source"] == expected for word in normalized))

    def test_auth_proxy_forwards_only_validated_optional_template_fields(self):
        payload = {"project_id": "compose_" + "a" * 32, **body()}
        result = hq_cli_api.action_plan("video-compose-render", payload)
        self.assertEqual(body(), result["body"])
        self.assertEqual("video-compose:write", result["scope"])
        for extra in ({"url": "https://example.com"}, {"editorial_plan": {}}, {"template_id": "../x"}):
            with self.subTest(extra=extra), self.assertRaises(hq_cli_api.CLIAPIError):
                hq_cli_api.action_plan("video-compose-render", {**payload, **extra})


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.source = self.root / "source.mp4"; self.source.write_bytes(b"test-source")
        self.payload = editorial.project_input(project(), body())
        self.media = {"duration_ms": 5000, "width": 1080, "height": 1920,
                      "has_audio": True, "video_codec": "h264", "audio_codec": "aac"}

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_assets_and_deterministic_html_manifest(self):
        with mock.patch.object(editorial.media, "probe_media", return_value=self.media):
            one = editorial.prepare_workspace(self.source, self.payload, self.root / "one", renderer.TEMPLATE_ROOT)
            two = editorial.prepare_workspace(self.source, self.payload, self.root / "two", renderer.TEMPLATE_ROOT)
        self.assertEqual(one, two)
        self.assertEqual((self.root / "one/index.html").read_bytes(), (self.root / "two/index.html").read_bytes())
        self.assertEqual(hashlib.sha256(b"test-source").hexdigest(), one["source_sha256"])
        self.assertEqual(5, len(one["assets"]))

    def test_fails_closed_for_missing_font_or_hash_drift(self):
        with self.assertRaises(ValueError):
            editorial.verify_assets(self.root)
        folder = self.root / contract.TEMPLATE_ID; folder.mkdir()
        (folder / "gsap.min.js").write_bytes(b"drifted")
        with self.assertRaisesRegex(ValueError, "校验"):
            editorial.verify_assets(self.root)

    def test_source_must_be_regular_with_audio_and_portrait_frame(self):
        with self.assertRaises(ValueError):
            editorial._regular(self.root)
        for overrides in ({"has_audio": False}, {"duration_ms": 4000}, {"width": 1920, "height": 1080}):
            with mock.patch.object(editorial.media, "probe_media", return_value={**self.media, **overrides}), self.assertRaises(ValueError):
                editorial.prepare_workspace(self.source, self.payload, self.root / "project", renderer.TEMPLATE_ROOT)
        self.assertFalse((self.root / "project").exists())

    def test_symlink_input_is_rejected(self):
        with mock.patch.object(pathlib.Path, "is_symlink", return_value=True), self.assertRaises(ValueError):
            editorial._regular(self.source)

    def test_runtime_has_no_on_request_install_and_rejects_old_version(self):
        with mock.patch.dict("os.environ", {"VIDEO_COMPOSE_EDITORIAL_RUNTIME": str(self.root)}), self.assertRaises(ValueError):
            editorial.runtime_command()
        (self.root / "package.json").write_text(json.dumps({"name": "hyperframes", "version": "0.7.88"}))
        with mock.patch.dict("os.environ", {"VIDEO_COMPOSE_EDITORIAL_RUNTIME": str(self.root)}), self.assertRaisesRegex(ValueError, "0.8.33"):
            editorial.runtime_command()

    def test_failed_check_never_overwrites_existing_output(self):
        out = self.root / "final.mp4"; out.write_bytes(b"old-accepted")
        with mock.patch.object(editorial, "runtime_command", return_value=(["node", "cli.js"], "chrome")), \
             mock.patch.object(editorial.media, "probe_media", return_value=self.media), \
             mock.patch.object(editorial, "_run_owned", side_effect=subprocess.TimeoutExpired("check", 1)), \
             self.assertRaises(ValueError):
            editorial.render(self.source, self.payload, out, renderer.TEMPLATE_ROOT)
        self.assertEqual(b"old-accepted", out.read_bytes())
        self.assertFalse(list(self.root.glob("editorial-*")))

    def test_runtime_rejects_old_node_before_accepting_work(self):
        (self.root / "package.json").write_text(json.dumps({"name": "hyperframes", "version": "0.8.33", "bin": "cli.js"}))
        (self.root / "cli.js").write_text("")
        with mock.patch.dict("os.environ", {"VIDEO_COMPOSE_EDITORIAL_RUNTIME": str(self.root),
                "VIDEO_COMPOSE_EDITORIAL_NODE": "node", "VIDEO_COMPOSE_EDITORIAL_BROWSER": str(self.source)}), \
             mock.patch.object(editorial.subprocess, "check_output", return_value=b"v18.20.0"), \
             self.assertRaisesRegex(ValueError, "Node 22"):
            editorial.runtime_command()

    def test_timeout_terminates_only_owned_process_tree(self):
        process = mock.Mock(pid=12345, returncode=0)
        if sys.platform.startswith("linux"):
            process.returncode = 124
            process.communicate.return_value = (b"", b"")
        else:
            process.communicate.side_effect = [subprocess.TimeoutExpired("render", 1), (b"", b"")]
        process.poll.return_value = None
        with mock.patch.object(editorial.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(editorial.os, "killpg", create=True) as killpg, \
             mock.patch.object(editorial.subprocess, "run") as taskkill, self.assertRaises(subprocess.TimeoutExpired):
            editorial._run_owned(["node", "render"], self.root, {}, 1)
        if os.name == "nt":
            self.assertEqual(["taskkill", "/PID", "12345", "/T", "/F"], taskkill.call_args.args[0])
            killpg.assert_not_called()
        else:
            self.assertEqual(sys.executable, popen.call_args.args[0][0])
            self.assertIn("editorial_process_supervisor.py", popen.call_args.args[0][2])
            self.assertEqual(["1", "--", "node", "render"], popen.call_args.args[0][3:])
            killpg.assert_not_called()
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            taskkill.assert_not_called()
        if sys.platform.startswith("linux"):
            process.kill.assert_not_called()
            self.assertEqual(1, process.communicate.call_count)
        else:
            process.kill.assert_called_once()
            self.assertEqual(2, process.communicate.call_count)

    def test_linux_browser_socket_temp_path_is_shallow_and_task_owned(self):
        evidence = pathlib.PurePosixPath('/home/runner/work/_temp/editorial-process-cleanup')
        suffix = '/com.google.Chrome.vkAUz5/SingletonSocket'
        old_path = str(evidence / '0-timeout-graceful') + suffix
        self.assertGreaterEqual(len(old_path.encode()), 108)
        with mock.patch.object(process_cleanup_check.tempfile, 'TemporaryDirectory') as temp:
            process_cleanup_check.socket_temp_directory(evidence)
        temp.assert_called_once_with(prefix='ep-', dir=evidence.parent)
        new_path = str(evidence.parent / 'ep-12345678') + suffix
        self.assertLess(len(new_path.encode()), 108)

    def test_linux_cleanup_budget_keeps_reaper_and_blocks_new_work(self):
        process = mock.Mock(pid=12345)
        process.communicate.side_effect = subprocess.TimeoutExpired("supervisor", 16)
        with mock.patch.object(editorial, "_CLEANUP_FAILED", threading.Event()), \
             mock.patch.object(editorial.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(editorial.threading, "Thread") as thread, \
             self.assertLogs(editorial.__name__, level="CRITICAL"):
            with self.assertRaisesRegex(subprocess.SubprocessError, "supervisor retained"):
                editorial._run_owned_linux(["node", "render"], self.root, {}, 1)
            with self.assertRaisesRegex(subprocess.SubprocessError, "new work disabled"):
                editorial._run_owned_linux(["node", "render"], self.root, {}, 1)
            self.assertEqual(1, popen.call_count)
            process.kill.assert_not_called()
            process.terminate.assert_called_once()
            thread.return_value.start.assert_called_once()
            with mock.patch.object(editorial.sys, "platform", "linux"), \
                 self.assertRaisesRegex(ValueError, "暂停新任务"):
                editorial.runtime_command()

    def test_subreaper_initialization_failure_never_starts_command(self):
        with mock.patch.object(supervisor, "_enable_subreaper", side_effect=OSError("prctl denied")), \
             mock.patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(OSError, "prctl denied"):
                supervisor.run(["node", "render"], 1)
            popen.assert_not_called()

    def test_subreaper_procfs_failure_never_starts_command(self):
        children = mock.Mock()
        children.read_text.side_effect = PermissionError("procfs denied")
        with mock.patch.object(supervisor, "_enable_subreaper", return_value=children), \
             mock.patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(PermissionError, "procfs denied"):
                supervisor.run(["node", "render"], 1)
            popen.assert_not_called()

    def test_prctl_failure_is_not_ignored(self):
        libc = mock.Mock()
        libc.prctl.return_value = -1
        with mock.patch.object(supervisor.sys, "platform", "linux"), \
             mock.patch.object(supervisor.ctypes, "CDLL", return_value=libc), \
             mock.patch.object(supervisor.ctypes, "get_errno", return_value=1), \
             mock.patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(OSError, "PR_SET_CHILD_SUBREAPER"):
                supervisor.run(["node", "render"], 1)
            popen.assert_not_called()

    def test_unexpected_supervisor_death_disables_retries(self):
        process = mock.Mock(pid=12345, returncode=-9)
        process.communicate.return_value = (b"", b"")
        with mock.patch.object(editorial, "_CLEANUP_FAILED", threading.Event()), \
             mock.patch.object(editorial.subprocess, "Popen", return_value=process) as popen, \
             self.assertLogs(editorial.__name__, level="CRITICAL"):
            with self.assertRaises(subprocess.CalledProcessError):
                editorial._run_owned_linux(["node", "render"], self.root, {}, 1)
            with self.assertRaisesRegex(subprocess.SubprocessError, "new work disabled"):
                editorial._run_owned_linux(["node", "render"], self.root, {}, 1)
            self.assertEqual(1, popen.call_count)


class Handler:
    def __init__(self, payload):
        self.payload = payload
        self.result = None
        self.path = ""

    def _json_body_strict(self):
        return self.payload

    def _send(self, status, payload):
        self.result = (status, payload)

    def _token(self):
        return "fixture"


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.dbpatch = mock.patch.object(store, "DB_PATH", str(self.root / "projects.db")); self.dbpatch.start()
        self.assets = self.root / "assets.db"
        with closing(self.asset_db()) as db:
            db.execute("""CREATE TABLE video_assets(id INTEGER PRIMARY KEY,job_id INTEGER,username TEXT,mode TEXT,
                video_file TEXT,video_url TEXT,text TEXT,resolution TEXT,ratio TEXT,motion TEXT,phase TEXT,
                model TEXT,status TEXT,error TEXT,created_at INTEGER,updated_at INTEGER)""")
            db.execute("INSERT INTO video_assets(id,username,mode,video_file,text,status) VALUES(1,'alice','import','source.mp4','fixture','done')")
            db.commit()
        _, revision, snapshot = api._source_asset(self.asset_db, "alice", 1)
        current = store.create_project("alice", 1, revision, snapshot)
        detected = analysis.detect_candidates(5000, words())
        current = store.save_analysis("alice", current["id"], 1, detected, "a" * 64)
        decisions = {c["id"]: "keep" for c in current["candidates"]}
        edl = analysis.build_edl(5000, current["candidates"], decisions)
        self.project = store.confirm_edit_decisions("alice", current["id"], 2, decisions, edl)
        self.input = editorial.project_input(self.project, body())
        self.source = self.root / "source.mp4"; self.source.write_bytes(b"source")
        self.runtimepatch = mock.patch.object(editorial, "runtime_command", return_value=(["node", "cli.js"], "chrome")); self.runtimepatch.start()

    def tearDown(self):
        self.runtimepatch.stop(); self.dbpatch.stop(); self.temp.cleanup()

    def asset_db(self):
        connection = sqlite3.connect(self.assets); connection.row_factory = sqlite3.Row
        return connection

    def request(self, payload=None, username="alice"):
        handler = Handler(body() if payload is None else payload)
        handler.path = api.BASE_PATH + "/" + self.project["id"] + "/render"
        api.dispatch_http(handler, "POST", lambda token: {"username": username}, lambda user: False,
                          self.asset_db, lambda rel: self.source, self.root)
        return handler.result

    def test_owner_is_checked_before_any_work(self):
        with mock.patch.object(api._RENDER_EXECUTOR, "submit") as submit:
            self.assertEqual(404, self.request(username="bob")[0])
        submit.assert_not_called()

    def test_invalid_plan_never_mutates_revision_or_submits(self):
        plan = example_plan(); plan["captions"][0]["en"] = ""
        with mock.patch.object(api._RENDER_EXECUTOR, "submit") as submit:
            self.assertEqual(400, self.request(body(plan))[0])
        self.assertEqual(3, store.get_project("alice", self.project["id"])["revision"])
        submit.assert_not_called()

    def test_same_request_deduplicates_and_changed_plan_conflicts(self):
        with mock.patch.object(api._RENDER_EXECUTOR, "submit") as submit:
            first = self.request(); second = self.request()
            plan = example_plan(); plan["captions"][0]["en"] = "A person"
            changed = self.request(body(plan))
        api._EDITORIAL_SLOTS.release()  # mocked executor did not run its finally
        self.assertEqual((202, 202, 409), (first[0], second[0], changed[0]))
        self.assertEqual(first[1]["project"]["revision"], second[1]["project"]["revision"])
        self.assertEqual(self.input, first[1]["project"]["render_input"])
        self.assertEqual(1, submit.call_count)

    def test_replay_does_not_bypass_revision_guard(self):
        with mock.patch.object(api._RENDER_EXECUTOR, "submit") as submit:
            self.assertEqual(202, self.request()[0])
            for revision in (1, 2, 5, 100):
                request = body(); request["expected_revision"] = revision
                self.assertEqual(409, self.request(request)[0])
        api._EDITORIAL_SLOTS.release()
        self.assertEqual(1, submit.call_count)

    def test_submit_exception_releases_slot_and_marks_retryable_failure(self):
        with mock.patch.object(api._RENDER_EXECUTOR, "submit", side_effect=RuntimeError("executor closed")):
            self.assertEqual(400, self.request()[0])
        failed = store.get_project("alice", self.project["id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual(5, failed["revision"])

    def test_busy_queue_does_not_leave_rendering_forever(self):
        with mock.patch.object(api, "_EDITORIAL_SLOTS") as slots:
            slots.acquire.return_value = False
            self.assertEqual(400, self.request()[0])
        self.assertEqual("failed", store.get_project("alice", self.project["id"])["status"])

    def test_crash_recovery_requires_current_revision(self):
        current, started = store.begin_render("alice", self.project["id"], 3, self.input)
        self.assertTrue(started)
        self.assertEqual(1, store.recover_interrupted_renders())
        with self.assertRaises(store.RevisionConflict):
            store.begin_render("alice", self.project["id"], 3, self.input)
        retried, started = store.begin_render("alice", self.project["id"], 5, self.input)
        self.assertTrue(started); self.assertEqual(6, retried["revision"])

    def test_atomic_concurrent_submission_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: store.begin_render("alice", self.project["id"], 3, self.input), range(4)))
        self.assertEqual(1, sum(started for _, started in values))
        self.assertTrue(all(value["revision"] == 4 for value, _ in values))

    def test_quality_failure_and_deleted_source_create_no_asset(self):
        current, _ = store.begin_render("alice", self.project["id"], 3, self.input)
        with mock.patch.object(api.media, "build_clean_master"), \
             mock.patch.object(api.renderer, "render", return_value={"template_id": contract.TEMPLATE_ID,
                 "template_version": contract.TEMPLATE_VERSION}), \
             mock.patch.object(api.media, "inspect_quality", return_value={"decision": "failed"}):
            api._run_render("alice", current["id"], 4, self.input, self.asset_db, lambda rel: self.source, self.root)
        self.assertEqual("failed", store.get_project("alice", current["id"])["status"])
        with closing(self.asset_db()) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM video_assets").fetchone()[0])
            db.execute("UPDATE video_assets SET status='deleted' WHERE id=1"); db.commit()
        store.begin_render("alice", current["id"], 5, self.input)
        with mock.patch.object(api.renderer, "render") as render:
            api._run_render("alice", current["id"], 6, self.input, self.asset_db, lambda rel: self.source, self.root)
        render.assert_not_called()

    def test_completed_replay_returns_same_asset_and_rejects_changed_template(self):
        store.begin_render("alice", self.project["id"], 3, self.input)
        def write_clean(src, edl, output):
            pathlib.Path(output).write_bytes(b"clean")
        def render(clean, payload, output):
            pathlib.Path(output).write_bytes(b"render")
            return {"template_id": contract.TEMPLATE_ID, "template_version": contract.TEMPLATE_VERSION,
                    "build_manifest": {"html_sha256": "b" * 64}}
        with mock.patch.object(api.media, "build_clean_master", side_effect=write_clean), \
             mock.patch.object(api.renderer, "render", side_effect=render), \
             mock.patch.object(api.media, "inspect_quality", return_value={"decision": "passed"}):
            api._run_render("alice", self.project["id"], 4, self.input, self.asset_db, lambda rel: self.source, self.root)
        replay = self.request()
        self.assertEqual(200, replay[0]); self.assertEqual("completed", replay[1]["project"]["status"])
        self.assertEqual(2, replay[1]["project"]["output_asset_id"])
        with closing(self.asset_db()) as db:
            self.assertEqual("720p", db.execute("SELECT resolution FROM video_assets WHERE id=2").fetchone()[0])
        handler = Handler(None)
        with mock.patch.object(api, "_default_render_input", return_value={"template_id": "clean-talking-v1"}):
            status, _ = self.request({"expected_revision": 3, "template_id": "clean-talking-v1"})
        self.assertEqual(409, status)


if __name__ == "__main__":
    unittest.main()
