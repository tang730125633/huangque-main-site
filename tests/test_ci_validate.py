from pathlib import Path, PurePosixPath
from unittest import TestCase

from scripts.ci_validate import (
    candidate_paths,
    check_redlines,
    is_dynamic_or_external,
    load_json_strict,
    parse_json_strict,
)


class RedlineTests(TestCase):
    def test_rejects_private_data_and_credentials(self) -> None:
        files = [
            PurePosixPath("data/leads.csv"),
            PurePosixPath("browser_data/cookies.json"),
            PurePosixPath("server/jobs.db"),
            PurePosixPath("config/.env.production"),
            PurePosixPath("deploy/private.key"),
        ]

        self.assertEqual(len(check_redlines(files)), len(files))

    def test_allows_normal_project_files(self) -> None:
        files = [
            PurePosixPath("site/index.html"),
            PurePosixPath("server/app.py"),
            PurePosixPath("docs/部署记录.md"),
        ]

        self.assertEqual(check_redlines(files), [])


class HtmlReferenceTests(TestCase):
    def test_extensionless_workbench_link_resolves_to_html(self) -> None:
        source = Path("site/workbench/audio.html")
        candidates = candidate_paths(source, "dashboard")

        self.assertIn(Path("site/workbench/dashboard.html"), candidates)

    def test_external_and_dynamic_references_are_ignored(self) -> None:
        self.assertTrue(is_dynamic_or_external("https://example.com/a.png"))
        self.assertTrue(is_dynamic_or_external("${result.url}"))
        self.assertTrue(is_dynamic_or_external("#pricing"))
        self.assertFalse(is_dynamic_or_external("../assets/cloud.css?v=8"))
class StrictJsonTests(TestCase):
    def test_rejects_duplicate_object_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate JSON key: schema"):
            parse_json_strict('{"schema": 1, "schema": 2}')

    def test_openapi_copies_are_strict_and_identical(self) -> None:
        docs = Path("docs/api/openapi.json")
        site = Path("site/api-docs/openapi.json")

        docs_spec = load_json_strict(docs)
        self.assertEqual(docs_spec, load_json_strict(site))
        profile = docs_spec["components"]["schemas"][
            "ShortDramaCharacterProfileMutation"
        ]
        self.assertEqual(
            {"type": "string", "minLength": 1, "maxLength": 20,
             "description": "可选的角色显示名称；同一项目内不可重复"},
            profile["properties"]["name"],
        )
