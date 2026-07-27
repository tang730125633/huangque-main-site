import unittest

from server.content_domains import short_drama_prompt_compiler as compiler


class ShortDramaPromptCompilerTests(unittest.TestCase):
    def test_visual_prompt_preserves_story_facts_and_forbids_generated_speech(self):
        source = "侦探在雨夜推开仓库门，近景，缓慢推进。"
        result = compiler.compile_visual_only_prompt(source)
        self.assertTrue(result["prompt"].startswith(source))
        self.assertIn("do not generate dialogue", result["prompt"])
        self.assertIn("do not generate", result["prompt"])
        self.assertIn("closed mouth", result["prompt"])
        self.assertEqual(
            compiler.PROMPT_TEMPLATE_VERSION,
            result["template_version"],
        )
        self.assertEqual(64, len(result["compiled_prompt_hash"]))

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            compiler.compile_visual_only_prompt(" ")


if __name__ == "__main__":
    unittest.main()
