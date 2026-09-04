# -*- coding: utf-8 -*-
"""三方能力合同：Agent 工具目录 / 执行白名单 / HQ CLI catalog。

守护目标：视频助手公开的每个能力都必须
1. 存在于 hq_cli_executor.ALLOWED_CAPABILITIES（否则任何调用前固定 403）；
2. 存在于 hq_cli.catalog.CAPABILITIES（真实 CLI 能力目录）；
3. 参数 schema 与真实 CLI 双向兼容（Agent 枚举 ⊆ CLI 枚举、边界一致），
   渠道规则与 CLI 目录严格相等，杜绝第三份漂移定义。
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = str(ROOT / "server")
CLI_SRC = str(ROOT / "tools" / "hq-cli" / "src")
for directory in (SERVER_DIR, CLI_SRC):
    if directory not in sys.path:
        sys.path.insert(0, directory)

from content_domains import hq_cli_executor, video_agent_tools  # noqa: E402
from hq_cli.catalog import CAPABILITIES, VIDEO_CHANNEL_RULES  # noqa: E402


class VideoAgentCapabilityContractTests(unittest.TestCase):
    def _specs(self):
        return video_agent_tools._SPECS

    def test_every_agent_capability_is_in_the_execution_allowlist(self):
        missing = sorted({
            spec["capability"] for spec in self._specs().values()
            if spec["capability"] not in hq_cli_executor.ALLOWED_CAPABILITIES
        })
        self.assertEqual([], missing)

    def test_every_agent_capability_exists_in_the_cli_catalog(self):
        missing = sorted({
            spec["capability"] for spec in self._specs().values()
            if spec["capability"] not in CAPABILITIES
        })
        self.assertEqual([], missing)

    def test_video_channel_rules_match_the_cli_catalog_exactly(self):
        # 渠道规则只有一份正本（hq_cli.catalog）；Agent 校验数据必须与它
        # 逐字节一致，任何一边改动都要先改目录并让契约测试通过。
        self.assertEqual(
            video_agent_tools.VIDEO_CHANNEL_RULES, VIDEO_CHANNEL_RULES
        )

    def test_video_generate_enums_match_the_cli_channel_union(self):
        spec = self._specs()["hq_quote_video_generate"]
        properties = spec["parameters"]["properties"]
        union_ratios = sorted({
            value for rule in VIDEO_CHANNEL_RULES.values() for value in rule["ratios"]
        })
        union_resolutions = sorted({
            value for rule in VIDEO_CHANNEL_RULES.values() for value in rule["resolutions"]
        })
        self.assertEqual(properties["ratio"]["enum"], union_ratios)
        self.assertEqual(properties["resolution"]["enum"], union_resolutions)
        self.assertEqual(
            properties["channel"]["enum"],
            sorted(VIDEO_CHANNEL_RULES),
        )
        # 真实 CLI 不接受 768p；21:9 / adaptive 是 Micro/MiniMax 合法值。
        self.assertNotIn("768p", union_resolutions)
        self.assertIn("21:9", union_ratios)
        self.assertIn("adaptive", union_ratios)

    def test_generate_audio_rejection_matches_cli_field_presence(self):
        # cli.py 按"字段是否存在"拒绝非 Micro 渠道，显式 false 也必须拒绝；
        # Agent 校验与之一致，防止模型补出可选布尔值后在下一层失败。
        for channel, rule in VIDEO_CHANNEL_RULES.items():
            if rule["generate_audio"]:
                continue
            for value in (True, False):
                with self.assertRaises(video_agent_tools.ToolError,
                                       msg="channel=%s value=%s" % (channel, value)):
                    video_agent_tools._validate_video_channel_rules(
                        {"channel": channel, "generate_audio": value},
                        video_agent_tools.VIDEO_CHANNEL_RULES,
                    )
        # micro 是唯一允许 generate_audio 的渠道，false 也放行。
        for value in (True, False):
            video_agent_tools._validate_video_channel_rules(
                {"channel": "micro", "generate_audio": value},
                video_agent_tools.VIDEO_CHANNEL_RULES,
            )

    def test_quote_tool_schemas_never_accept_more_than_the_cli(self):
        for tool_name, spec in self._specs().items():
            capability = spec["capability"]
            cli_spec = CAPABILITIES.get(capability)
            if not cli_spec:
                continue
            cli_schema = cli_spec.get("input_schema") or {}
            cli_properties = cli_schema.get("properties") or {}
            agent_properties = spec["parameters"].get("properties") or {}
            for field, agent_schema in agent_properties.items():
                cli_field = cli_properties.get(field)
                if not cli_field:
                    continue
                agent_enum = agent_schema.get("enum")
                cli_enum = cli_field.get("enum")
                if agent_enum and cli_enum:
                    extra = sorted(set(agent_enum) - set(cli_enum))
                    self.assertEqual(
                        [], extra,
                        "%s.%s 接受了 CLI 必拒的值" % (tool_name, field),
                    )
                for bound in ("minimum", "maximum", "minItems", "maxItems"):
                    if bound in agent_schema and bound in cli_field:
                        agent_value, cli_value = agent_schema[bound], cli_field[bound]
                        if bound.startswith("min"):
                            self.assertGreaterEqual(
                                agent_value, cli_value,
                                "%s.%s.%s 比 CLI 更宽松" % (tool_name, field, bound),
                            )
                        else:
                            self.assertLessEqual(
                                agent_value, cli_value,
                                "%s.%s.%s 比 CLI 更宽松" % (tool_name, field, bound),
                            )

    def test_tryon_specs_match_the_cli_fields(self):
        fast = self._specs()["hq_quote_tryon_fast_video"]
        classic = self._specs()["hq_quote_tryon_classic_video"]
        cli_fast = CAPABILITIES["tryon-fast-generate"]["input_schema"]
        cli_classic = CAPABILITIES["tryon-classic-generate"]["input_schema"]
        self.assertEqual(
            set(fast["parameters"]["required"]), set(cli_fast["required"])
        )
        self.assertEqual(
            set(classic["parameters"]["required"]), set(cli_classic["required"])
        )
        self.assertEqual(
            fast["parameters"]["properties"]["seconds"]["minimum"],
            cli_fast["properties"]["seconds"]["minimum"],
        )
        self.assertEqual(
            classic["parameters"]["properties"]["seconds"]["maximum"],
            cli_classic["properties"]["seconds"]["maximum"],
        )

    def test_read_tool_schemas_stay_inside_cli_bounds(self):
        assets = self._specs()["hq_list_assets"]["parameters"]["properties"]
        tasks = self._specs()["hq_list_tasks"]["parameters"]["properties"]
        cli_assets = CAPABILITIES["assets"]["input_schema"]["properties"]
        cli_tasks = CAPABILITIES["tasks"]["input_schema"]["properties"]
        self.assertTrue(
            set(assets["kind"]["enum"]) <= set(cli_assets["kind"]["enum"])
        )
        self.assertGreaterEqual(
            tasks["days"]["minimum"], cli_tasks["days"]["minimum"]
        )
        self.assertGreaterEqual(
            tasks["page_size"]["minimum"], cli_tasks["page_size"]["minimum"]
        )


if __name__ == "__main__":
    unittest.main()
