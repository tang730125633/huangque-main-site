"""Offline client contract, no production requests."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from hq_cli import cli, client, editorial_contract as contract


def request():
    return {"project_id": "compose_" + "a" * 32, "expected_revision": 3,
        "template_id": contract.TEMPLATE_ID, "editorial_plan": {
            "schema": contract.SCHEMA_ID, "timebase": "edited_output",
            "transcript_hash": "a" * 64, "edit_decision_version": 1,
            "title": ["口播网感", "内容有价值"],
            "captions": [{"start": 0, "end": 3, "text": "内容有价值", "en": "Content matters"}],
            "keywords": ["内容"], "camera": [{"at": 0, "scale": 1, "transition": "cut"}],
            "keyword_punches": [{"at": 0, "word": "内容", "strength": 1.075}], "callouts": []}}


class EditorialCliTests(unittest.TestCase):
    def test_complete_and_legacy_requests(self):
        cap = cli.CAPABILITIES["video-compose-render"]
        cli._validate(cap, request())
        cli._validate(cap, {"project_id": "compose_" + "a" * 32, "expected_revision": 3})
        self.assertEqual("video-compose:write", cap["required_scope"])
        self.assertTrue(cap["confirmation_required"])

    def test_invalid_nested_data(self):
        for mutate in (
            lambda p: p.pop("editorial_plan"),
            lambda p: p.update(template_id="unknown"),
            lambda p: p["editorial_plan"]["captions"][0].pop("en"),
            lambda p: p["editorial_plan"]["captions"][0].update(en="假英文"),
            lambda p: p["editorial_plan"]["camera"][0].update(scale=10**400),
            lambda p: p["editorial_plan"]["camera"][0].update(at=float("nan")),
            lambda p: p["editorial_plan"]["captions"][0].update(text="\ud800"),
            lambda p: p["editorial_plan"].update(command="arbitrary"),
        ):
            payload = request(); mutate(payload)
            with self.subTest(payload=repr(payload)), self.assertRaises(cli.CliError):
                cli._validate(cli.CAPABILITIES["video-compose-render"], payload)

    def test_zero_candidate_review(self):
        cli._validate(cli.CAPABILITIES["video-compose-review"],
            {"project_id": "compose_" + "a" * 32, "expected_revision": 2, "decisions": {}})

    def test_cli_forwarding_and_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HQ_CLI_CONFIG_DIR": folder}):
            for key in ("HQ_CLI_ACCESS_TOKEN", "HQ_CLI_API_BASE", "HQ_CLI_QUOTE_TOKEN"):
                os.environ.pop(key, None)
            client.save_credentials("t" * 43, 2000000000, cli.LOGIN_SCOPES)
            def invoke(confirm):
                output, error = io.StringIO(), io.StringIO()
                stream = type("Input", (), {"buffer": io.BytesIO(json.dumps(request()).encode())})()
                args = ["run", "video-compose-render", "--input", "@-"]
                if confirm:
                    args.append("--confirm")
                with patch("sys.stdout", output), patch("sys.stderr", error), patch("sys.stdin", stream):
                    return cli.main(args), error.getvalue()
            with patch("hq_cli.client.request_json") as http:
                code, _ = invoke(False)
                self.assertNotEqual(0, code)
                http.assert_not_called()
                http.return_value = (202, {"project": {"id": request()["project_id"], "status": "rendering"}, "accepted": True})
                code, error = invoke(True)
                self.assertEqual(0, code, error)
                sent = http.call_args.kwargs["body"]
                self.assertEqual(request(), sent["input"])
                self.assertIs(True, sent["confirm"])


if __name__ == "__main__":
    unittest.main()
