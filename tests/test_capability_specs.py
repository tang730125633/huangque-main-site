import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "hermes_ip12"))
sys.path.insert(0, str(ROOT / "tools" / "hq-cli" / "src"))

import capability_specs
import hq_cli
from hq_cli.catalog import capability_list
import hq_cli_api
import agent_runtime


class CapabilitySpecRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = capability_specs.load_registry()
        cls.by_id = {item["id"]: item for item in cls.registry["capabilities"]}

    def test_public_cli_0105_and_action_catalog_are_exact_projections(self):
        self.assertEqual(hq_cli.__version__, "0.10.5")
        expected_cli = sorted(capability_list(), key=lambda item: item["id"])
        expected_actions = sorted(hq_cli_api.ACTION_CATALOG, key=lambda item: item["action"])
        self.assertEqual(len(expected_cli), 88)
        self.assertEqual(capability_specs.compiled_projection("hq_cli", self.registry), expected_cli)
        self.assertEqual(capability_specs.compiled_projection("action_catalog", self.registry), expected_actions)
        self.assertEqual(self.registry["source_contracts"]["hq_cli_schema"], "hq.capabilities/v1")
        self.assertEqual(self.registry["source_contracts"]["action_catalog_version"], "hq-action-catalog-v3")

    def test_migration_is_repeatable_and_generated_registry_is_current(self):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "migrate_capability_specs.py"), "--check"],
            cwd=ROOT, check=True,
        )
        tools = [item for item in self.registry["capabilities"] if item["kind"] == "tool"]
        self.assertEqual(len(tools), 90)
        self.assertEqual(sum(item.get("projection", {}).get("hq_cli") is not None for item in tools), 88)
        self.assertEqual(
            {item["id"] for item in tools if item.get("projection", {}).get("hq_cli") is None},
            {"voice-clone-create", "voice-clone-status"},
        )
        for tool in tools:
            action = (tool.get("projection") or {}).get("action_catalog")
            if action:
                self.assertEqual(tool["input_schema"], action["input_schema"])
        self.assertIn("file", self.by_id["image-upload"]["input_schema"]["properties"])
        self.assertIn("file", self.by_id["video-upload"]["input_schema"]["properties"])

    def test_governance_rejects_duplicates_dangling_refs_and_unsafe_paid_or_async_tools(self):
        duplicate = copy.deepcopy(self.registry)
        duplicate["capabilities"].append(copy.deepcopy(duplicate["capabilities"][0]))
        with self.assertRaisesRegex(capability_specs.CapabilitySpecError, "unique"):
            capability_specs.validate_registry(duplicate)

        dangling = copy.deepcopy(self.registry)
        outcome = next(item for item in dangling["capabilities"] if item["kind"] == "outcome")
        outcome["harness"]["allowed_tool_refs"][0] = "missing.tool"
        with self.assertRaisesRegex(capability_specs.CapabilitySpecError, "dangling"):
            capability_specs.validate_registry(dangling)

        paid = copy.deepcopy(self.registry)
        tool = next(item for item in paid["capabilities"]
                    if item["kind"] == "tool" and item["side_effect"] == "paid")
        tool["confirmation"]["required"] = False
        with self.assertRaisesRegex(capability_specs.CapabilitySpecError, "quote-card"):
            capability_specs.validate_registry(paid)

        async_missing_poll = copy.deepcopy(self.registry)
        tool = next(item for item in async_missing_poll["capabilities"]
                    if item["kind"] == "tool" and item["async"]["enabled"])
        tool["async"]["poll_tool_ref"] = None
        with self.assertRaisesRegex(capability_specs.CapabilitySpecError, "poll"):
            capability_specs.validate_registry(async_missing_poll)

        async_dangling_poll = copy.deepcopy(self.registry)
        tool = next(item for item in async_dangling_poll["capabilities"]
                    if item["kind"] == "tool" and item["async"]["enabled"])
        tool["async"]["poll_tool_ref"] = "missing.poll"
        with self.assertRaisesRegex(capability_specs.CapabilitySpecError, "dangling poll"):
            capability_specs.validate_registry(async_dangling_poll)

    def test_lifecycle_is_one_way(self):
        self.assertTrue(capability_specs.lifecycle_transition_allowed("draft", "experimental"))
        self.assertTrue(capability_specs.lifecycle_transition_allowed("stable", "deprecated"))
        self.assertFalse(capability_specs.lifecycle_transition_allowed("stable", "draft"))
        self.assertFalse(capability_specs.lifecycle_transition_allowed("retired", "stable"))

    def test_master_discovers_outcome_specialist_gets_only_resolved_tools(self):
        catalog = capability_specs.discover_outcomes(self.registry)
        self.assertEqual([item["id"] for item in catalog], ["matrix-video.text-media-text"])
        self.assertNotIn("allowed_tool_refs", catalog[0])
        binding = capability_specs.resolve_outcome(
            "matrix-video.text-media-text",
            account_tool_ids={item["id"] for item in capability_list()},
            context={
                "top_text": "主食零食鲜食怎么分",
                "bottom_text": "关注我，一起轻松喂养",
                "media_asset_ids": [101, 102],
            },
            registry=self.registry,
        )
        self.assertEqual(binding["runtime_tool_ids"], [
            "account", "channels", "assets", "pricing", "image-upload", "video-upload", "task",
        ])
        self.assertEqual(binding["model_tool_ids"], ["account", "channels", "assets", "pricing", "task"])
        self.assertNotIn("text-video-generate", binding["runtime_tool_ids"])
        self.assertEqual(binding["status"], "blocked")
        self.assertEqual(binding["blockers"], ["matrix_video_main_site_api_missing"])

    def test_zero_cost_path_requests_one_missing_input_then_stops_at_real_api_blocker(self):
        available = {item["id"] for item in capability_list()}
        missing = capability_specs.resolve_outcome(
            "matrix-video.text-media-text", account_tool_ids=available,
            context={"bottom_text": "关注我", "media_asset_ids": [1]}, registry=self.registry,
        )
        self.assertEqual(missing["status"], "needs_input")
        self.assertEqual(missing["missing"], ["top_text"])
        self.assertEqual(missing["invalid"], ["media_asset_ids"])
        self.assertFalse(any(self.by_id[item]["side_effect"] == "paid" for item in missing["runtime_tool_ids"]))

        invalid = capability_specs.resolve_outcome(
            "matrix-video.text-media-text", account_tool_ids=available,
            context={"top_text": 7, "bottom_text": "关注我", "media_asset_ids": [True, "bad"]},
            registry=self.registry,
        )
        self.assertEqual(invalid["status"], "needs_input")
        self.assertEqual(invalid["invalid"], ["top_text", "media_asset_ids"])

        invalid_optional = capability_specs.resolve_outcome(
            "matrix-video.text-media-text", account_tool_ids=available,
            context={
                "top_text": "上", "bottom_text": "下", "media_asset_ids": [1, 2],
                "template_id": 123, "bgm": "yes", "surprise": True,
            },
            registry=self.registry,
        )
        self.assertEqual(invalid_optional["status"], "needs_input")
        self.assertEqual(
            invalid_optional["invalid"],
            ["unknown:surprise", "template_id", "bgm"],
        )

    def test_agent_run_freezes_registry_and_capability_versions(self):
        binding = capability_specs.resolve_outcome(
            "matrix-video.text-media-text",
            account_tool_ids={item["id"] for item in capability_list()},
            context={"top_text": "上", "bottom_text": "下", "media_asset_ids": [1, 2]},
            registry=self.registry,
        )

        class Policy:
            agent_id = "matrix_video_text_media_text_agent"

        project = {"id": "project_1"}
        run = agent_runtime.start(
            project, "run_1", Policy(), "制作模板视频",
            project_id="project_1", capability_binding=binding,
        )
        self.assertEqual(run["registry_version"], self.registry["registry_version"])
        self.assertEqual(run["capability_binding"]["runtime_tool_ids"], binding["runtime_tool_ids"])
        replay = agent_runtime.start(
            project, "run_1", Policy(), "制作模板视频",
            project_id="project_1", capability_binding=binding,
        )
        self.assertEqual(replay["capability_binding"]["binding_digest"], binding["binding_digest"])
        changed = copy.deepcopy(binding)
        changed["registry_version"] = "changed"
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "binding mismatch"):
            agent_runtime.start(
                project, "run_1", Policy(), "制作模板视频",
                project_id="project_1", capability_binding=changed,
            )

        forged = copy.deepcopy(binding)
        forged["runtime_tool_ids"] = forged["runtime_tool_ids"][:-1]
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "digest mismatch"):
            agent_runtime.start(
                {"id": "project_2"}, "run_2", Policy(), "制作模板视频",
                project_id="project_2", capability_binding=forged,
            )

        missing_specialist = copy.deepcopy(binding)
        missing_specialist.pop("specialist_id")
        unsigned = copy.deepcopy(missing_specialist)
        unsigned.pop("binding_digest")
        missing_specialist["binding_digest"] = capability_specs._digest(unsigned)
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "specialist mismatch"):
            agent_runtime.start(
                {"id": "project_3"}, "run_3", Policy(), "制作模板视频",
                capability_binding=missing_specialist,
            )

        fake_legacy = copy.deepcopy(binding)
        fake_legacy["binding_digest"] = "legacy"
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "legacy.*mismatch"):
            agent_runtime.start(
                {"id": "project_4"}, "run_4", Policy(), "制作模板视频",
                capability_binding=fake_legacy,
            )

    def test_legacy_agent_run_is_backfilled_before_resume(self):
        class Policy:
            agent_id = "legacy_agent"
            allowed_tools = ("project.read",)

            @staticmethod
            def next_action(run):
                if run.get("step"):
                    return {"type": "wait", "awaiting": "input"}
                return {"type": "tool", "tool": "project.read", "input": {}}

        project = {"id": "project_1", "agent_runs": {"run_1": {
            "schema": agent_runtime.RUN_SCHEMA, "id": "run_1", "run_id": "run_1",
            "agent_id": "legacy_agent", "status": "planning", "step": 0,
            "tool_calls": {}, "observations": [], "inputs": {}, "events": [],
            "event_sequence": 0, "revision": 1,
            "_private": {"job_id": None, "quote_token": "", "tool_inputs": {}, "tool_results": {}},
        }}}
        tools = agent_runtime.ToolRegistry()
        tools.register("project.read", lambda _payload: {"source": {}})
        run = agent_runtime.start(project, "run_1", Policy(), "恢复旧工单")
        self.assertEqual(run["capability_binding"]["runtime_tool_ids"], ["project.read"])
        resumed = agent_runtime.resume(project, "run_1", Policy(), tools)
        self.assertEqual(resumed["status"], "needs_input")

    def test_runtime_rejects_a_tool_outside_the_frozen_binding(self):
        class Policy:
            agent_id = "bounded_agent"

            @staticmethod
            def next_action(_run):
                return {"type": "tool", "tool": "project.read", "input": {}}

        binding = {
            "schema": "huangque.outcome-binding/v1",
            "registry_version": "registry_1",
            "outcome_id": "bounded.outcome",
            "outcome_version": "1.0.0",
            "specialist_id": "bounded_agent",
            "runtime_tool_ids": ["capability.read"],
            "model_tool_ids": ["capability.read"],
            "capability_versions": {"capability.read": "1.0.0"},
        }
        binding["binding_digest"] = capability_specs._digest(binding)
        project = {"id": "project_1"}
        agent_runtime.start(project, "run_1", Policy(), "受限执行", capability_binding=binding)
        tools = agent_runtime.ToolRegistry()
        tools.register("project.read", lambda _payload: {"source": {}})
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "not allowed"):
            agent_runtime.resume(project, "run_1", Policy(), tools)

    def test_runtime_revalidates_binding_before_every_resume(self):
        class Policy:
            agent_id = "bounded_agent"

            @staticmethod
            def next_action(_run):
                return {"type": "tool", "tool": "project.writeback", "input": {
                    "production_id": "prod_1", "artifact_digest": "sha256:x",
                }}

        binding = {
            "schema": "huangque.outcome-binding/v1",
            "registry_version": "registry_1",
            "outcome_id": "bounded.outcome",
            "outcome_version": "1.0.0",
            "specialist_id": "bounded_agent",
            "runtime_tool_ids": ["capability.read"],
            "model_tool_ids": ["capability.read"],
            "capability_versions": {"capability.read": "1.0.0"},
        }
        binding["binding_digest"] = capability_specs._digest(binding)
        project = {"id": "project_1"}
        run = agent_runtime.start(project, "run_1", Policy(), "受限执行", capability_binding=binding)
        run["capability_binding"]["runtime_tool_ids"].append("project.writeback")
        tools = agent_runtime.ToolRegistry()
        tools.register("project.writeback", lambda _payload: {"artifact_id": "a1"})
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "digest mismatch"):
            agent_runtime.resume(project, "run_1", Policy(), tools)


if __name__ == "__main__":
    unittest.main()
