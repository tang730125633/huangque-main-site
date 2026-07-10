import json
import pathlib
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TASKS_JS = (ROOT / "site/workbench/tasks.js").read_text(encoding="utf-8")
SHELL_JS = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
VIDEO_HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class GlobalTaskStoreTests(unittest.TestCase):
    def test_task_store_keeps_video_jobs_active_until_terminal(self):
        harness = textwrap.dedent(
            """
            const fs = require('fs');
            global.window = globalThis;
            global.CustomEvent = function(type, init) { this.type = type; this.detail = init && init.detail; };
            const events = [];
            window.addEventListener = function() {};
            window.dispatchEvent = function(event) { events.push(event.type); };
            global.location = { pathname: '/workbench/video.html', href: 'https://example.test/workbench/video.html' };
            global.history = { replaceState: function() {} };
            const values = {};
            global.localStorage = {
              getItem: function(key) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
              setItem: function(key, value) { values[key] = String(value); }
            };
            global.document = {
              readyState: 'complete',
              querySelector: function() { return null; },
              getElementById: function() { return null; }
            };
            eval(fs.readFileSync('site/workbench/tasks.js', 'utf8'));
            HQTasks.upsert({ id: 42, kind: 'leads', status: 'running', label: '获客任务' });
            HQTasks.upsert({ id: 42, kind: 'video', status: 'processing', label: '影视级模仿' });
            const active = {
              total: HQTasks.activeCount(),
              video: HQTasks.activeCount('video'),
              latest: HQTasks.latestActive('video'),
              ready: events.includes('hq:tasks-ready')
            };
            HQTasks.upsert({ id: 42, kind: 'video', status: 'done' });
            process.stdout.write(JSON.stringify({
              active,
              doneCount: HQTasks.activeCount('video'),
              leadsCount: HQTasks.activeCount('leads'),
              leadsLabel: HQTasks.get(42, 'leads').label
            }));
            """
        )
        result = subprocess.run(
            ["node", "-e", harness],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        state = json.loads(result.stdout)
        self.assertEqual(state["active"]["total"], 2)
        self.assertEqual(state["active"]["video"], 1)
        self.assertEqual(state["active"]["latest"]["label"], "影视级模仿")
        self.assertTrue(state["active"]["ready"])
        self.assertEqual(state["doneCount"], 0)
        self.assertEqual(state["leadsCount"], 1)
        self.assertEqual(state["leadsLabel"], "获客任务")

    def test_shared_shell_loads_tracker_for_every_workbench_page(self):
        self.assertIn("function loadTaskTracker()", SHELL_JS)
        self.assertIn("script[data-hq-tasks]", SHELL_JS)
        self.assertIn("script.src='tasks.js'", SHELL_JS)
        self.assertIn("buildLoginModal(); loadTaskTracker();", SHELL_JS)

    def test_tracker_routes_video_jobs_and_supports_current_shell(self):
        self.assertIn('processing: 1', TASKS_JS)
        self.assertIn('document.querySelector(".hq-aside nav")', TASKS_JS)
        self.assertIn('el.style.display = nav ? "flex" : ""', TASKS_JS)
        self.assertIn('"video.html?task=" + id', TASKS_JS)
        self.assertIn('new CustomEvent("hq:resume-task"', TASKS_JS)


class VideoTaskIntegrationTests(unittest.TestCase):
    def test_all_video_submit_paths_register_the_returned_job(self):
        self.assertEqual(VIDEO_HTML.count("trackVideoJob(res.data.job_id"), 4)
        for label in ("影视级模仿", "换装换背景视频", "数字人口播", "label:label"):
            self.assertIn(label, VIDEO_HTML)

    def test_polling_persists_progress_and_terminal_states(self):
        self.assertIn("if(tries===0) resumedVideoTaskId=String(id);", VIDEO_HTML)
        self.assertIn("if(r.ok) return d;", VIDEO_HTML)
        self.assertIn("httpError.jobTerminal=r.status===404", VIDEO_HTML)
        self.assertIn("status:d.status||'running'", VIDEO_HTML)
        self.assertIn("{status:'done',phase:'done'}", VIDEO_HTML)
        self.assertIn("{status:'failed',lastError:e.message||'生成失败'}", VIDEO_HTML)
        self.assertIn("{status:'pending',lastError:'前台轮询已超时'}", VIDEO_HTML)
        self.assertIn("任务仍在后台生成，可点侧栏任务继续", VIDEO_HTML)

    def test_video_page_resumes_query_or_latest_active_job(self):
        self.assertIn("new URLSearchParams(location.search).get('task')", VIDEO_HTML)
        self.assertIn("tasks.get(id,'video')", VIDEO_HTML)
        self.assertIn("tasks.latestActive('video')", VIDEO_HTML)
        self.assertIn("window.addEventListener('hq:resume-task'", VIDEO_HTML)
        self.assertIn("resumeTrackedVideoTask();", VIDEO_HTML)


if __name__ == "__main__":
    unittest.main()
