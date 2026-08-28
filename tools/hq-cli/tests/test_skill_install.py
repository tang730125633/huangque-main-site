import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hq_cli import cli, skill_install


SKILL = b"---\nname: use-huangque-cli\ndescription: Test Skill.\n---\n\n# Test\n"
OPENAI_YAML = b'interface:\n  display_name: "Huangque CLI"\n'


def manifest_bytes(skill=SKILL, openai_yaml=OPENAI_YAML):
    manifest = {
        "schema": "huangque.agent-skill/v1",
        "skill": {"name": "use-huangque-cli", "version": "0.1.1"},
        "cli": {"minimum": "0.10.2", "tested": "0.10.2", "latest": "0.10.2", "installer": "0.11.0"},
        "source_ref": "v0.1.1",
        "files": [
            {
                "path": "skills/use-huangque-cli/SKILL.md",
                "sha256": hashlib.sha256(skill).hexdigest(),
            },
            {
                "path": "skills/use-huangque-cli/agents/openai.yaml",
                "sha256": hashlib.sha256(openai_yaml).hexdigest(),
            },
        ],
        "adapters": {
            "deepseek": {"destination": "~/.dsh/skills/use-huangque-cli"},
            "codex": {"destination": "~/.codex/skills/use-huangque-cli"},
            "openclaw": {"destination": "~/.openclaw/skills/use-huangque-cli"},
            "pi": {"destination": "~/.pi/agent/skills/use-huangque-cli"},
            "mcp": {"command": "hq", "args": ["mcp"], "minimum_cli": "0.11.0"},
        },
    }
    return json.dumps(manifest).encode()


def fixture_fetch(skill=SKILL, openai_yaml=OPENAI_YAML):
    manifest = manifest_bytes(skill, openai_yaml)

    def fetch(url, max_bytes):
        if url == skill_install.MANIFEST_URL:
            return manifest
        if url.endswith("/SKILL.md"):
            return skill
        if url.endswith("/agents/openai.yaml"):
            return openai_yaml
        raise AssertionError("unexpected URL: %s" % url)

    return fetch


FIXTURE_MANIFEST_SHA256 = hashlib.sha256(manifest_bytes()).hexdigest()


class SkillInstallTests(unittest.TestCase):
    def test_manifest_is_pinned_to_the_reviewed_skill_release(self):
        self.assertEqual("0.1.1", skill_install.SKILL_VERSION)
        self.assertIn(skill_install.SKILL_COMMIT, skill_install.MANIFEST_URL)
        self.assertEqual(40, len(skill_install.SKILL_COMMIT))
        self.assertRegex(skill_install.MANIFEST_SHA256, r"^[0-9a-f]{64}$")

    def test_installs_one_canonical_skill_into_each_agent_root(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            expected = {
                "deepseek": home / ".dsh/skills/use-huangque-cli",
                "codex": home / ".codex/skills/use-huangque-cli",
                "openclaw": home / ".openclaw/skills/use-huangque-cli",
                "pi": home / ".pi/agent/skills/use-huangque-cli",
            }
            for target, destination in expected.items():
                with self.subTest(target=target):
                    result = skill_install.install_skill(
                        target, home=home, fetch=fixture_fetch(),
                        _manifest_sha256=FIXTURE_MANIFEST_SHA256,
                    )
                    self.assertEqual("installed", result["status"])
                    self.assertEqual(SKILL, (destination / "SKILL.md").read_bytes())
                    self.assertEqual(OPENAI_YAML, (destination / "agents/openai.yaml").read_bytes())
                    self.assertEqual("0.1.1", json.loads(
                        (destination / ".huangque-skill.json").read_text(encoding="utf-8")
                    )["version"])

    def test_repeat_install_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            first = skill_install.install_skill(
                "codex", home=temp, fetch=fixture_fetch(), _manifest_sha256=FIXTURE_MANIFEST_SHA256,
            )
            second = skill_install.install_skill(
                "codex", home=temp, fetch=fixture_fetch(), _manifest_sha256=FIXTURE_MANIFEST_SHA256,
            )
        self.assertEqual("installed", first["status"])
        self.assertEqual("current", second["status"])

    def test_unmanaged_skill_requires_replace_and_is_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / ".dsh/skills/use-huangque-cli"
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("personal changes", encoding="utf-8")
            with self.assertRaises(skill_install.SkillInstallError) as raised:
                skill_install.install_skill(
                    "deepseek", home=temp, fetch=fixture_fetch(), _manifest_sha256=FIXTURE_MANIFEST_SHA256,
                )
            self.assertEqual("skill_replace_required", raised.exception.error)
            result = skill_install.install_skill(
                "deepseek", replace=True, home=temp, fetch=fixture_fetch(),
                _manifest_sha256=FIXTURE_MANIFEST_SHA256,
            )
            self.assertEqual("updated", result["status"])
            self.assertEqual("personal changes", (Path(result["backup"]) / "SKILL.md").read_text(encoding="utf-8"))

    def test_hash_mismatch_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as temp:
            fetch = fixture_fetch()

            def corrupted(url, max_bytes):
                return b"corrupt" if url.endswith("/SKILL.md") else fetch(url, max_bytes)

            with self.assertRaises(skill_install.SkillInstallError) as raised:
                skill_install.install_skill(
                    "pi", home=temp, fetch=corrupted, _manifest_sha256=FIXTURE_MANIFEST_SHA256,
                )
            self.assertEqual("skill_hash_error", raised.exception.error)
            self.assertFalse((Path(temp) / ".pi/agent/skills/use-huangque-cli").exists())

    def test_manifest_rejects_duplicate_or_extra_files(self):
        manifest = json.loads(manifest_bytes())
        manifest["files"].append(dict(manifest["files"][0]))

        def fetch(url, max_bytes):
            return json.dumps(manifest).encode() if url == skill_install.MANIFEST_URL else SKILL

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(skill_install.SkillInstallError) as raised:
                skill_install.install_skill(
                    "codex", home=temp, fetch=fetch,
                    _manifest_sha256=hashlib.sha256(json.dumps(manifest).encode()).hexdigest(),
                )
        self.assertEqual("skill_manifest_error", raised.exception.error)

    def test_mcp_entry_uses_the_current_hq_process(self):
        result = skill_install.install_skill(
            "mcp", fetch=fixture_fetch(), _manifest_sha256=FIXTURE_MANIFEST_SHA256,
        )
        self.assertEqual("available", result["status"])
        self.assertEqual({"command": "hq", "args": ["mcp"]}, result["server"])

    def test_manifest_hash_mismatch_is_rejected_before_parsing(self):
        with self.assertRaises(skill_install.SkillInstallError) as raised:
            skill_install.install_skill(
                "codex", fetch=fixture_fetch(), _manifest_sha256="0" * 64,
            )
        self.assertEqual("skill_hash_error", raised.exception.error)

    def test_cli_surfaces_installer_result_as_json(self):
        with patch("hq_cli.skill_install.install_skill", return_value={
            "target": "pi", "status": "installed", "skill_version": "0.1.0", "destination": "/tmp/skill",
        }):
            with patch("sys.stdout") as stdout:
                stdout.write.side_effect = lambda value: None
                stdout.flush.side_effect = lambda: None
                self.assertEqual(0, cli.main(["skill", "install", "pi", "--json"]))


if __name__ == "__main__":
    unittest.main()
