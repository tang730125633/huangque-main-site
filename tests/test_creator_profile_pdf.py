import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from creator_agent.profile_agent import MODULES, initial_state
from creator_agent.profile_pdf import render_profile_pdf


class CreatorProfilePDFTests(unittest.TestCase):
    @staticmethod
    def profile_and_state():
        state = initial_state()
        answers = {}
        selected = {}
        reviews = {}
        for module in range(1, 5):
            answers[str(module)] = {
                question["key"]: "真实背景回答"
                for question in MODULES[module]["questions"]
            }
            selected[str(module)] = {
                "title": "模块%d方案" % module,
                "one_liner": "基于真实经历建立可信内容定位",
                "strengths": ["优势%d" % index for index in range(1, 4)],
                "risks": ["风险%d" % index for index in range(1, 4)],
            }
            reviews[str(module)] = {
                "summary": "模块%d结构化总结" % module,
            }
        state.update({
            "answers": answers,
            "selected_profiles": selected,
            "module_reviews": reviews,
            "completed_modules": [1, 2, 3, 4],
            "profile_ready": True,
            "phase": "ready",
        })
        return {"answers": answers, "modules": selected}, state

    def render(self, profile, state):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = pathlib.Path(directory.name) / "profile.pdf"
        result = render_profile_pdf(output, "PDF 压力测试", profile, state)
        self.assertTrue(output.is_file())
        self.assertGreater(result["size"], 1024)
        self.assertEqual(output.read_bytes()[:5], b"%PDF-")
        return output

    def test_legal_1600_character_answer_can_span_pages(self):
        profile, state = self.profile_and_state()
        first = MODULES[1]["questions"][0]["key"]
        state["answers"]["1"][first] = "长" * 1600
        profile["answers"] = state["answers"]
        self.render(profile, state)

    def test_maximum_candidate_strengths_and_risks_can_span_pages(self):
        profile, state = self.profile_and_state()
        profile["modules"]["1"]["strengths"] = ["优" * 240 for _ in range(6)]
        profile["modules"]["1"]["risks"] = ["险" * 240 for _ in range(6)]
        state["selected_profiles"] = profile["modules"]
        self.render(profile, state)


if __name__ == "__main__":
    unittest.main()
