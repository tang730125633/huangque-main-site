import pathlib
import shutil
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/digital-human-oneclick.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/digital-human-unified.js").read_text(encoding="utf-8")
STATE_SCRIPT = ROOT / "site/workbench/digital-human-unified-state.js"
DIRECTOR = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")


class DigitalHumanPrecisionUiTests(unittest.TestCase):
    def test_precision_controls_are_available_inside_the_unified_page(self):
        for target in (
            "photoDrop", "voiceUploadDrop", "customerMaterialsPicker",
            "driveAudioDrop", "dhDrop", "dhConsent", "dhAnalyze", "dhStart",
        ):
            self.assertIn('id="%s"' % target, PAGE)

    def test_director_entry_opens_photo_mode_without_a_second_photo_tab(self):
        self.assertIn('data-active="script"', PAGE)
        self.assertIn('href="digital-human-oneclick.html">🎬 数字人一键生成</a>', DIRECTOR)
        self.assertIn('data-dh-mode="photo">数字人一键生成</button>', PAGE)
        self.assertIn('data-dh-mode="video">真人视频 Precision</button>', PAGE)
        self.assertNotIn("照片数字人", PAGE)
        self.assertIn("params.get('mode')==='video'?'video':'photo'", SCRIPT)
        self.assertNotIn('data-mode="script_to_video"', DIRECTOR)

    def test_video_mode_clones_the_uploaded_videos_voice_before_paid_generation(self):
        state_tag = 'src="digital-human-unified-state.js?'
        app_tag = 'src="digital-human-unified.js?'
        self.assertLess(PAGE.index(state_tag), PAGE.index(app_tag))
        for marker in (
            "/api/gen/video/lipsync-import",
            "/api/gen/video/lipsync-voice-sample",
            "/api/gen/audio/slots",
            "/api/gen/audio/clone-vip",
            "/api/gen/audio/clone-status",
            "/api/gen/audio",
            "/api/gen/video",
            "/api/gen/video-compose/projects",
            "/analyze-source",
            "/edit-decisions",
            "/render",
        ):
            self.assertIn(marker, SCRIPT)
        self.assertNotIn("/api/gen/audio/voices", SCRIPT)
        self.assertNotIn('id="dhVoice"', PAGE)
        self.assertIn("lipsync_mode:'precision'", SCRIPT)
        self.assertIn("dynamic_duration:true", SCRIPT)
        self.assertIn("consent_confirmed:true", SCRIPT)
        self.assertIn("'Idempotency-Key'", SCRIPT)
        analyze = SCRIPT[SCRIPT.index("function analyzeVoice("):SCRIPT.index("function previewVoice(")]
        self.assertNotIn("generateAudio(", analyze)
        self.assertNotIn("generateLipsync(", analyze)
        confirm = SCRIPT[SCRIPT.index("function confirmAndGenerate("):]
        self.assertIn("generateAudio(text).then(generateLipsync)", confirm)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_ready_voice_slot_numbering_executes_selection_and_rejects_invalid_input(self):
        harness = textwrap.dedent(
            r"""
            const assert = require('assert');
            const fs = require('fs');
            const vm = require('vm');

            const appSource = fs.readFileSync(process.argv[1], 'utf8');
            const stateApi = require(process.argv[2]);
            const availableStart = appSource.indexOf('function availableSlot()');
            const pollStart = appSource.indexOf('function pollClone(', availableStart);
            const analyzeStart = appSource.indexOf('function analyzeVoice(', pollStart);
            const previewStart = appSource.indexOf('function previewVoice(', analyzeStart);
            assert(availableStart >= 0 && pollStart > availableStart);
            assert(analyzeStart > pollStart && previewStart > analyzeStart);
            const behaviorSource = appSource.slice(availableStart, pollStart)
              + '\n' + appSource.slice(analyzeStart, previewStart);
            const slots = [
              {slot_id: 'internal-slot-alpha-very-long', status: 'ready', voice_name: '第一音色'},
              {slot_id: 'internal-slot-beta-very-long', status: 'ready', voice_name: '第二音色'},
              {slot_id: 'internal-slot-gamma-very-long', status: 'ready', voice_name: '最后音色'},
            ];

            async function exercise(answer, confirmResult) {
              const requests = [];
              const prompts = [];
              const confirms = [];
              const elements = {
                dhScript: {value: '测试文案'},
                dhConsent: {checked: true},
                dhAnalyze: {disabled: false},
              };
              const context = {
                DigitalHumanUnifiedState: stateApi,
                state: {source: {id: 42}, running: false, retry: 0, runId: '', sample: null},
                fresh: value => value,
                request: (path, options) => {
                  requests.push({path, options});
                  if (path === '/api/gen/audio/slots') return Promise.resolve({items: slots});
                  if (path === '/api/gen/video/lipsync-voice-sample') {
                    return Promise.reject(new Error('stop-after-sample'));
                  }
                  return Promise.reject(new Error('unexpected request: ' + path));
                },
                window: {
                  prompt: (message, initial) => {
                    prompts.push({message, initial});
                    return answer;
                  },
                  confirm: message => {
                    confirms.push(message);
                    return confirmResult;
                  },
                },
                $: id => elements[id] || (elements[id] = {}),
                resetVoice: () => {},
                simpleHash: () => 'stable-test-run',
                setStage: () => {},
                setStep: () => {},
                setVoiceStep: () => {},
                status: () => {},
                Date,
                Number,
                String,
                Object,
                Promise,
              };
              vm.createContext(context);
              vm.runInContext(behaviorSource, context, {filename: 'digital-human-unified.behavior.js'});
              context.analyzeVoice(false);
              for (let attempt = 0; attempt < 20 && context.state.running; attempt += 1) {
                await new Promise(resolve => setImmediate(resolve));
              }
              assert.strictEqual(context.state.running, false, 'analysis promise did not settle');
              return {requests, prompts, confirms};
            }

            (async () => {
              for (const [answer, expectedId, expectedNumber] of [
                ['1', slots[0].slot_id, 1],
                ['3', slots[2].slot_id, 3],
              ]) {
                const result = await exercise(answer, true);
                const sample = result.requests.find(
                  item => item.path === '/api/gen/video/lipsync-voice-sample'
                );
                assert(sample, 'valid selection must reach the sample endpoint');
                assert.strictEqual(sample.options.body.slot_id, expectedId);
                assert.strictEqual(result.confirms.length, 1);
                assert(result.confirms[0].includes('编号 ' + expectedNumber));
                for (const slot of slots) assert(!result.confirms[0].includes(slot.slot_id));
                const prompt = result.prompts[0].message;
                assert(prompt.includes('1 — 第一音色'));
                assert(prompt.includes('3 — 最后音色'));
                for (const slot of slots) assert(!prompt.includes(slot.slot_id));
              }

              for (const answer of ['', null, '0', '4', '1.5', 'abc']) {
                const result = await exercise(answer, true);
                assert.deepStrictEqual(
                  result.requests.map(item => item.path),
                  ['/api/gen/audio/slots'],
                  'invalid input must stop before the paid sample endpoint'
                );
                assert.strictEqual(result.confirms.length, 0);
              }

              const cancelled = await exercise('1', false);
              assert.deepStrictEqual(
                cancelled.requests.map(item => item.path),
                ['/api/gen/audio/slots'],
                'cancel must stop before the paid sample endpoint'
              );
              assert.strictEqual(cancelled.confirms.length, 1);
              console.log('voice slot numbering behavior tests passed');
            })().catch(error => {
              console.error(error && error.stack || error);
              process.exit(1);
            });
            """
        )
        completed = subprocess.run(
            [
                "node", "-e", harness,
                str(ROOT / "site/workbench/digital-human-unified.js"),
                str(STATE_SCRIPT),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("voice slot numbering behavior tests passed", completed.stdout)

    def test_three_templates_have_visible_ten_second_examples(self):
        expected = {
            "viral-talking-head-v1": "high-frequency-10s.mp4",
            "professional-explainer-v1": "professional-explainer-10s.mp4",
            "clean-talking-v1": "clean-talking-10s.mp4",
        }
        for template_id, filename in expected.items():
            self.assertIn('data-template="%s"' % template_id, PAGE)
            self.assertIn(filename, PAGE)
            preview = ROOT / "site/assets/one-click/previews" / filename
            self.assertGreater(preview.stat().st_size, 100000)
        self.assertEqual(3, PAGE.count('<video muted loop playsinline preload="metadata"'))

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_javascript_parses(self):
        for script in (STATE_SCRIPT, ROOT / "site/workbench/digital-human-unified.js"):
            completed = subprocess.run(
                ["node", "--check", str(script)], capture_output=True, text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_slot_selection_and_clone_version_idempotency_behaviors(self):
        completed = subprocess.run(
            ["node", str(ROOT / "tests/test_digital_human_unified_state.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("unified state tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
