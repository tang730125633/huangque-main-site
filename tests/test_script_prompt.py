import pathlib
import unittest


TEXT_PY = pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/text.py"


class ScriptPromptStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEXT_PY.read_text(encoding="utf-8")

    def test_style_line_mapping_exists(self):
        """_STYLE_LINE 字典包含口播/剧情/种草三种风格"""
        self.assertIn("_STYLE_LINE", self.source,
                      "text.py 应有 _STYLE_LINE 风格→line含义映射")
        self.assertIn('"口播"', self.source,
                      "应包含口播风格")
        self.assertIn('"剧情"', self.source,
                      "应包含剧情风格")
        self.assertIn('"种草"', self.source,
                      "应包含种草风格")

    def test_line_desc_var_used_in_prompt(self):
        """line_desc 变量被拼入 prompt 字符串"""
        self.assertIn("line_desc", self.source,
                      "prompt 应使用 line_desc 变量代替硬编码的'口播台词'")

    def test_prompt_no_longer_hardcodes_talking_only(self):
        """prompt 不再硬编码 '口播台词' 或 '口播口语化有钩子'"""
        self.assertNotIn("口播口语化有钩子可直接念", self.source,
                         "不应再硬编码口播专用提示")

    def test_json_structure_unchanged(self):
        """JSON 输出结构仍是 {scenes:[{dur,scene,line}]}"""
        self.assertIn('"scenes"', self.source,
                      "返回结构应仍包含 scenes 数组")
        self.assertIn('"dur"', self.source,
                      "每个分镜应仍包含 dur 字段")


if __name__ == "__main__":
    unittest.main()
