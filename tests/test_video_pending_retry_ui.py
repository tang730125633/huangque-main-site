import json
import pathlib
import shutil
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is required for video retry behavior tests")
class VideoPendingRetryUiTests(unittest.TestCase):
    def test_pending_request_restores_exact_body_across_urls(self):
        saved = {
            "channel": "minimax",
            "prompt": "keep the original request",
            "source_inspiration_id": 1234567,
            "model": "MiniMax-H3",
            "ratio": "16:9",
            "duration": 10,
            "resolution": "2k",
        }
        harness = textwrap.dedent(
            r"""
            const fs = require('fs');
            const html = fs.readFileSync('site/workbench/video.html', 'utf8');
            const pageScript = html.match(/<script>\s*\(function\(\)\{([\s\S]*?)\}\)\(\);\s*<\/script>/)[1];
            const retryScript = pageScript.slice(
              pageScript.indexOf('function canonicalOfficialRetryValue'),
              pageScript.indexOf('function saveSoraRetry')
            );
            const savedBody = process.env.HQ_TEST_SAVED_BODY;
            const sessionValues = {
              hq_minimax_pending_submit: JSON.stringify({
                key: 'minimax-web-original-key', body: savedBody, blocked: false
              })
            };
            const localValues = {};
            function storage(values) {
              return {
                getItem(key) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
                setItem(key, value) { values[key] = String(value); },
                removeItem(key) { delete values[key]; }
              };
            }
            global.window = globalThis;
            window.crypto = { randomUUID() { return 'new-random-key'; } };
            global.location = { search: '', href: 'https://example.test/workbench/video.html' };
            global.sessionStorage = storage(sessionValues);
            global.localStorage = storage(localValues);
            const elements = {
              minimaxPrompt: { value: '' }, minimaxStatus: { textContent: '' }
            };
            function $(id) { return elements[id] || null; }
            function toast() {}
            function updateOfficialVideoControls() {}
            function setSubmitLock() {}
            function setBusy() {}
            function renderVideoDrafts() {}
            const requests = [];
            function fetch(url, options) {
              requests.push({ url, options });
              return new Promise(function() {});
            }
            var token = '__cookie__';
            var officialVideoHealthKnown = true;
            var omniAvailable = true, seedanceAvailable = true, minimaxAvailable = true;
            var selectedOmniRatio = '16:9', selectedOmniDuration = 5;
            var selectedMiniMaxRatio = '9:16', selectedMiniMaxDuration = 15;
            var selectedSeedanceModel = 'doubao-seedance-2-0-260128';
            var selectedSeedanceRatio = 'adaptive', selectedSeedanceDuration = 5;
            var selectedSeedanceResolution = '720p', selectedSeedanceUpscale = false;
            var selectedSeedanceAudio = true;
            var selectedGrokRatio = '16:9', selectedGrokDuration = 10;
            var selectedGrokResolution = '720p', selectedGrokModel = 'grok-imagine-video';
            var grokOperation = 'generate', grokEditVideoData = '', grokEditDuration = 0;
            var grokRefData = [], microRefData = [], omniRefData = [], minimaxRefData = [];
            var currentVideoDraft = null;
            var OFFICIAL_VIDEO_RETRY_STORAGE = {
              omni: 'hq_omni_pending_submit', micro: 'hq_seedance_pending_submit',
              minimax: 'hq_minimax_pending_submit'
            };
            var OFFICIAL_VIDEO_BLOCK_STORAGE = {
              omni: 'hq_omni_pending_block', micro: 'hq_seedance_pending_block',
              minimax: 'hq_minimax_pending_block'
            };
            var officialVideoRetry = {
              omni: {key: '', body: '', blocked: false},
              micro: {key: '', body: '', blocked: false},
              minimax: {key: '', body: '', blocked: false}
            };
            eval(retryScript);
            restoreOfficialVideoRetries();
            submitXiaole('minimax');
            process.stdout.write(JSON.stringify({
              requestCount: requests.length,
              body: requests[0] && requests[0].options.body,
              prompt: elements.minimaxPrompt.value
            }));
            """
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            cwd=ROOT,
            env={**dict(__import__("os").environ), "HQ_TEST_SAVED_BODY": json.dumps(saved, ensure_ascii=False)},
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        observed = json.loads(result.stdout)
        self.assertEqual(1, observed["requestCount"])
        self.assertEqual(saved, json.loads(observed["body"]))
        self.assertEqual(saved["prompt"], observed["prompt"])

    def test_user_can_discard_pending_request_and_submit_with_a_new_key(self):
        html = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('id="minimaxDiscardPendingBtn"', html)
        saved = {
            "channel": "minimax",
            "prompt": "start again after abandoning",
            "model": "MiniMax-H3",
            "ratio": "9:16",
            "duration": 15,
            "resolution": "2k",
        }
        harness = textwrap.dedent(
            r"""
            const fs = require('fs');
            const html = fs.readFileSync('site/workbench/video.html', 'utf8');
            const pageScript = html.match(/<script>\s*\(function\(\)\{([\s\S]*?)\}\)\(\);\s*<\/script>/)[1];
            const retryScript = pageScript.slice(
              pageScript.indexOf('function canonicalOfficialRetryValue'),
              pageScript.indexOf('function saveSoraRetry')
            );
            const savedBody = process.env.HQ_TEST_SAVED_BODY;
            const sessionValues = {
              hq_minimax_pending_submit: JSON.stringify({
                key: 'minimax-web-original-key', body: savedBody, blocked: false
              })
            };
            const localValues = {
              hq_minimax_pending_block: JSON.stringify({
                key: 'minimax-web-original-key', blocked: true
              })
            };
            function storage(values) {
              return {
                getItem(key) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
                setItem(key, value) { values[key] = String(value); },
                removeItem(key) { delete values[key]; }
              };
            }
            function classes(initial) {
              const values = new Set(initial || []);
              return {
                toggle(name, force) { force ? values.add(name) : values.delete(name); },
                contains(name) { return values.has(name); }
              };
            }
            global.window = globalThis;
            window.crypto = { randomUUID() { return 'new-random-key'; } };
            const confirmMessages = [];
            const confirmDecisions = [false, true];
            window.confirm = function(message) {
              confirmMessages.push(String(message));
              return confirmDecisions.shift();
            };
            global.location = { search: '', href: 'https://example.test/workbench/video.html' };
            global.sessionStorage = storage(sessionValues);
            global.localStorage = storage(localValues);
            const elements = {
              minimaxPrompt: { value: '', focused: false, focus() { this.focused = true; } },
              minimaxStatus: { textContent: '' },
              minimaxPendingActions: { classList: classes(['hidden']) },
              minimaxDiscardPendingBtn: { disabled: true, onclick: null }
            };
            function $(id) { return elements[id] || null; }
            const toastMessages = [];
            function toast(message) { toastMessages.push(message); }
            function updateOfficialVideoControls() {}
            function setSubmitLock() {}
            function setBusy() {}
            function renderVideoDrafts() {}
            const requests = [];
            function fetch(url, options) {
              requests.push({ url, options });
              return new Promise(function() {});
            }
            var token = '__cookie__';
            var officialVideoHealthKnown = true;
            var omniAvailable = true, seedanceAvailable = true, minimaxAvailable = true;
            var selectedOmniRatio = '16:9', selectedOmniDuration = 5;
            var selectedMiniMaxRatio = '9:16', selectedMiniMaxDuration = 15;
            var selectedSeedanceModel = 'doubao-seedance-2-0-260128';
            var selectedSeedanceRatio = 'adaptive', selectedSeedanceDuration = 5;
            var selectedSeedanceResolution = '720p', selectedSeedanceUpscale = false;
            var selectedSeedanceAudio = true;
            var selectedGrokRatio = '16:9', selectedGrokDuration = 10;
            var selectedGrokResolution = '720p', selectedGrokModel = 'grok-imagine-video';
            var grokOperation = 'generate', grokEditVideoData = '', grokEditDuration = 0;
            var grokRefData = [], microRefData = [], omniRefData = [], minimaxRefData = [];
            var currentVideoDraft = null;
            var OFFICIAL_VIDEO_RETRY_STORAGE = {
              omni: 'hq_omni_pending_submit', micro: 'hq_seedance_pending_submit',
              minimax: 'hq_minimax_pending_submit'
            };
            var OFFICIAL_VIDEO_BLOCK_STORAGE = {
              omni: 'hq_omni_pending_block', micro: 'hq_seedance_pending_block',
              minimax: 'hq_minimax_pending_block'
            };
            var officialVideoRetry = {
              omni: {key: '', body: '', blocked: false},
              micro: {key: '', body: '', blocked: false},
              minimax: {key: '', body: '', blocked: false}
            };
            eval(retryScript);
            bindOfficialVideoRetryControls();
            restoreOfficialVideoRetries();
            const availableBeforeDiscard = {
              hidden: elements.minimaxPendingActions.classList.contains('hidden'),
              disabled: elements.minimaxDiscardPendingBtn.disabled
            };
            elements.minimaxDiscardPendingBtn.onclick();
            const cancelledDiscard = {
              sessionPreserved: Object.prototype.hasOwnProperty.call(sessionValues, 'hq_minimax_pending_submit'),
              localPreserved: Object.prototype.hasOwnProperty.call(localValues, 'hq_minimax_pending_block'),
              statePreserved: officialVideoRetry.minimax.key === 'minimax-web-original-key',
              hidden: elements.minimaxPendingActions.classList.contains('hidden'),
              disabled: elements.minimaxDiscardPendingBtn.disabled
            };
            elements.minimaxDiscardPendingBtn.onclick();
            const discarded = {
              sessionCleared: !Object.prototype.hasOwnProperty.call(sessionValues, 'hq_minimax_pending_submit'),
              localCleared: !Object.prototype.hasOwnProperty.call(localValues, 'hq_minimax_pending_block'),
              hidden: elements.minimaxPendingActions.classList.contains('hidden'),
              disabled: elements.minimaxDiscardPendingBtn.disabled,
              stateCleared: !officialVideoRetry.minimax.key && !officialVideoRetry.minimax.body,
              promptFocused: elements.minimaxPrompt.focused,
              status: elements.minimaxStatus.textContent
            };
            submitXiaole('minimax');
            const newStored = JSON.parse(sessionValues.hq_minimax_pending_submit || 'null');
            process.stdout.write(JSON.stringify({
              availableBeforeDiscard, cancelledDiscard, discarded, confirmMessages,
              requestCount: requests.length,
              requestKey: requests[0] && requests[0].options.headers['Idempotency-Key'],
              newStored,
              toast: toastMessages[toastMessages.length - 1]
            }));
            """
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            cwd=ROOT,
            env={**dict(__import__("os").environ), "HQ_TEST_SAVED_BODY": json.dumps(saved, ensure_ascii=False)},
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        observed = json.loads(result.stdout)
        self.assertEqual({"hidden": False, "disabled": False}, observed["availableBeforeDiscard"])
        self.assertEqual({
            "sessionPreserved": True,
            "localPreserved": True,
            "statePreserved": True,
            "hidden": False,
            "disabled": False,
        }, observed["cancelledDiscard"])
        self.assertEqual(2, len(observed["confirmMessages"]))
        self.assertTrue(all("扣点" in message and "任务列表" in message
                            for message in observed["confirmMessages"]))
        self.assertTrue(all([
            observed["discarded"]["sessionCleared"],
            observed["discarded"]["localCleared"],
            observed["discarded"]["hidden"],
            observed["discarded"]["disabled"],
            observed["discarded"]["stateCleared"],
            observed["discarded"]["promptFocused"],
        ]))
        self.assertIn("已放弃", observed["discarded"]["status"])
        self.assertEqual(1, observed["requestCount"])
        self.assertNotEqual("minimax-web-original-key", observed["requestKey"])
        self.assertEqual(observed["requestKey"], observed["newStored"]["key"])


if __name__ == "__main__":
    unittest.main()
