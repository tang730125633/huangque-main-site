# -*- coding: utf-8 -*-
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
os.environ["CONTENT_OUT"] = tempfile.mkdtemp(prefix="huangque-prompt-opt-")
imggen = importlib.import_module("imggen_api")


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "精华液产品特写，柔光微距，镜头缓慢推进，高级广告质感"}]}}]}).encode()


class PromptOptimizeTests(unittest.TestCase):
    def test_video_optimization_uses_existing_fast_gemini_model(self):
        with patch.object(imggen, "GEMINI_KEY", "test-key"), patch.object(imggen.urllib.request, "urlopen", return_value=_Response()) as request:
            result = imggen.gen_prompt_optimize("精华液，高级", "video")
        self.assertIn("镜头缓慢推进", result)
        body = json.loads(request.call_args.args[0].data.decode())
        self.assertEqual(0, body["generationConfig"]["thinkingConfig"]["thinkingBudget"])
        self.assertIn(imggen.REVERSE_MODEL, request.call_args.args[0].full_url)

    def test_image_and_video_buttons_use_the_existing_gemini_route(self):
        root = Path(__file__).resolve().parents[1]
        banana = (root / "site/workbench/banana.html").read_text(encoding="utf-8")
        video = (root / "site/workbench/video.html").read_text(encoding="utf-8")
        self.assertIn('id="bOptimize"', banana)
        self.assertIn("action:'optimize',prompt:source,kind:'image'", banana)
        self.assertIn('id="grokOptimizeBtn"', video)
        self.assertIn("action:'optimize',prompt:source,kind:'video'", video)


if __name__ == "__main__":
    unittest.main()
