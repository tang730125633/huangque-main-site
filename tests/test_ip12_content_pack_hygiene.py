import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"


class IP12ContentPackHygieneTests(unittest.TestCase):
    def test_rejects_and_redacts_structured_output_inside_scripts(self):
        script = r'''
import copy
import server

good = (
    "今天想聊一个很实际的问题：很多人学了不少 AI 工具，却一直没有把它放进真实工作。"
    "我的做法是先选一个每天都会遇到的小问题，再用最简单的流程跑通，记录哪里省了时间、哪里仍然需要人工判断。"
    "这样做不会让工具替你负责，但能让每一次尝试都留下可复用的方法。"
)
bad = (
    "资料中未包含模块 5 的第 1 条精选标题，暂不能生成完整口播文案。"
    "{ name: 开源协作, description: 缺少标题, topics: [{ title: null, script: 暂不能生成 }] }"
    + "}" * 80
)

def raw_pack(content):
    return {"categories": [{
        "name": f"种类{index}", "description": "精选理由",
        "topics": [{
            "title": f"选题{index}", "hook": "自然钩子",
            "objective": "建立信任", "script": content,
        }],
    } for index in range(1, 4)]}

assert server._content_script_rejection_reason(good) == ""
assert server._content_script_rejection_reason(bad)
try:
    server._normalize_content_pack(raw_pack(bad))
    raise AssertionError("structured output leak was accepted")
except ValueError:
    pass

pack = server._normalize_content_pack(raw_pack(good))
pack["categories"][1]["topics"][0]["versions"][-1]["content"] = bad
original = copy.deepcopy(pack)
assert not server._content_pack_ready(pack)
public = server._public_content_pack(pack)
assert public["output_valid"] is False
assert "格式异常" in public["output_error"]
assert "资料中未包含模块 5" not in str(public)
assert "}}}}" not in str(public)
assert pack == original

recovered = copy.deepcopy(pack)
recovered_versions = recovered["categories"][1]["topics"][0]["versions"]
recovered_versions.append({"version": 2, "content": good})
recovered_public = server._public_content_pack(recovered)
assert recovered_public.get("output_valid") is not False
assert "资料中未包含模块 5" not in str(recovered_public)

target = {"category_id": "category-2", "topic_id": "topic-2-01"}
try:
    server._production_source({"deliverables": {"6": pack}}, target)
    raise AssertionError("invalid script reached production source")
except server.coach_harness.HarnessError:
    pass

print("IP12_CONTENT_PACK_HYGIENE_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IP12_CONTENT_PACK_HYGIENE_OK", result.stdout)

    def test_frontend_stops_rendering_invalid_pack(self):
        page = (HERMES / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("item.output_valid===false", page)
        self.assertIn("文案格式异常，已停止展示", page)


if __name__ == "__main__":
    unittest.main()
