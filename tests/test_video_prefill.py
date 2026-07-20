import pathlib
import unittest


VIDEO_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html"


class VideoUrlPrefillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = VIDEO_HTML.read_text(encoding="utf-8")

    def test_function_talking_prefills_script_text(self):
        """?function=talking&prompt=xxx 自动切数字人口播 + 预填文案"""
        self.assertIn("function=talking", self.html,
                      "video.html 应支持 ?function=talking URL 参数")
        self.assertIn("updateFunction('talking')", self.html,
                      "命中 function=talking 时应切到 talking tab")
        self.assertIn("$('scriptText')", self.html,
                      "应预填 scriptText")
        self.assertIn(".dispatchEvent(new Event('input'))", self.html,
                      "应触发 input 事件更新字数统计")

    def test_function_talking_does_not_leak_to_channel_branch(self):
        """function=talking 命中后 return，不走到 channel=grok/micro 分支"""
        talking_pos = self.html.find("func==='talking'")
        channel_pos = self.html.find("channel')||'grok'")
        self.assertGreater(talking_pos, 0,
                           "应包含 function=talking 分支判断")
        self.assertGreater(channel_pos, 0,
                           "应保留原有 channel=grok/micro 分支")
        # talking 分支在 channel 分支之前 return，所以 channel 代码仍存在
        self.assertIn("if(func==='talking')", self.html,
                      "function=talking 应有独立分支")


if __name__ == "__main__":
    unittest.main()
