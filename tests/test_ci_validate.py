import ast
import subprocess
from pathlib import Path, PurePosixPath
from unittest import TestCase

from scripts.ci_validate import (
    candidate_paths,
    check_redlines,
    check_secret_literals,
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

    def test_rejects_hardcoded_production_secrets(self) -> None:
        path = Path("scripts/_secret_guard_test.py")
        path.write_text('QG_' + 'KEY = "not-a-real-key"\n', encoding="utf-8")
        try:
            errors = check_secret_literals([PurePosixPath(path)])
        finally:
            path.unlink()

        self.assertEqual(len(errors), 1)
        self.assertIn("QG_KEY", errors[0])
        self.assertNotIn("not-a-real-key", errors[0])

    def test_allows_environment_reads_and_placeholders(self) -> None:
        path = Path("scripts/_secret_guard_test.py")
        path.write_text(
            'QG_KEY = os.environ.get("QG_KEY", "")\n'
            'LEADGEN_WORKER_TOKEN=change-me\n',
            encoding="utf-8",
        )
        try:
            errors = check_secret_literals([PurePosixPath(path)])
        finally:
            path.unlink()

        self.assertEqual(errors, [])

    def test_rejects_worker_token_in_url(self) -> None:
        path = Path("worker/_secret_guard_test.py")
        path.write_text('url = "/api/claim?token=" + token\n', encoding="utf-8")
        try:
            errors = check_secret_literals([PurePosixPath(path)])
        finally:
            path.unlink()

        self.assertEqual(len(errors), 1)
        self.assertIn("X-Worker-Token", errors[0])


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
    def test_api_docs_safe_filter_keeps_only_active_reads(self) -> None:
        subprocess.run([
            "node", "-e", r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync('site/api-docs/index.html', 'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)].at(-1)[1];
const context = {
  window: {}, setInterval: () => {},
  document: {getElementById: () => null, querySelectorAll: () => []}
};
vm.createContext(context);
vm.runInContext(source, context);
const result = context.activeSafeSpec({
  info: {title: 'test', description: 'test'},
  paths: {
    '/safe': {get: {'x-hq-test-safety': 'safe-read', 'x-hq-runtime-status': 'active'}},
    '/pending': {get: {'x-hq-test-safety': 'safe-read', 'x-hq-runtime-status': 'pending-deployment'}},
    '/write': {post: {'x-hq-test-safety': 'state-write', 'x-hq-runtime-status': 'active'}}
  }
});
if (JSON.stringify(Object.keys(result.paths)) !== '["/safe"]') process.exit(1);
if (!result.info.description.includes('共 1 个操作')) process.exit(1);
"""
        ], check=True)

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
        operations = [
            operation
            for item in docs_spec["paths"].values()
            for method, operation in item.items()
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        audit = docs_spec["x-hq-document-audit"]
        self.assertEqual(len(docs_spec["paths"]), audit["total_paths"])
        self.assertEqual(len(operations), audit["total_operations"])
        for status, field in {
            "active": "active_operations",
            "pending-deployment": "production_pending_operations",
            "routing-unavailable": "production_routing_unavailable_operations",
            "deprecated": "deprecated_operations",
        }.items():
            self.assertEqual(
                sum(operation.get("x-hq-runtime-status") == status
                    for operation in operations),
                audit[field],
            )
        self.assertTrue(all(operation.get("x-hq-test-safety") for operation in operations))
        self.assertTrue(all(operation.get("x-hq-runtime-status") for operation in operations))

    def test_openapi_covers_registered_http_endpoints(self) -> None:
        from server.content_domains import function_registry

        spec = load_json_strict(Path("docs/api/openapi.json"))
        verbs = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}

        def endpoints(value):
            if isinstance(value, dict):
                method, path = value.get("method"), value.get("path")
                if method in verbs and isinstance(path, str) and path.startswith("/api/"):
                    yield method.lower(), path.split("?", 1)[0]
                for child in value.values():
                    yield from endpoints(child)
            elif isinstance(value, list):
                for child in value:
                    yield from endpoints(child)

        registered = set(endpoints(function_registry.list_pages()))
        documented = {
            (method, path)
            for path, item in spec["paths"].items()
            for method in item
            if method.upper() in verbs
        }
        self.assertEqual(77, len(registered))
        self.assertEqual(set(), registered - documented)
        tree = ast.parse(Path("server/content_domains/short_drama.py").read_text())
        route_table = ast.literal_eval(next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_HTTP_ROUTES"
                    for target in node.targets)
        ))
        short_drama_registered = {
            (method.lower(), path)
            for method, route_paths in route_table.items()
            for path in route_paths
        }
        self.assertEqual(134, len(short_drama_registered))
        self.assertEqual(set(), short_drama_registered - documented)

    def test_openapi_validates_live_action_import_and_role_saves(self) -> None:
        spec = load_json_strict(Path("docs/api/openapi.json"))
        schemas = spec["components"]["schemas"]
        contract = [{
            "character_key": "character_1", "name": "Lin Xia",
            "role_type": "main", "gender": "female", "identity_text": "clerk",
            "relationships": "", "personality": "calm", "age": "26",
            "face_shape": "oval", "hairstyle": "short", "hair_color": "black",
            "height_body": "165cm", "fixed_clothing": "white shirt",
            "fixed_colors": "white", "accessories": "watch",
            "appearance_prompt": "cinematic portrait",
            "wardrobe_prompt": "white shirt",
            "reference_views": ["front_full", "side_full", "back_full"],
        }]
        import_path = spec["paths"]["/api/gen/short-drama/projects/import"]["post"]
        self.assertIn("Idempotency-Key", [
            item.get("name") for item in import_path["parameters"]
        ])
        import_schema = import_path["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            255,
            schemas["ShortDramaImportRequest"]["properties"]["filename"]["maxLength"],
        )
        self.assertEqual(
            40,
            schemas["ShortDramaImportRequest"]["properties"]["genre"]["maxLength"],
        )
        self.assertEqual(
            ["", "complete_story"],
            schemas["ShortDramaImportRequest"]["properties"]["source_requirement"]["enum"],
        )
        for project_schema in ("ShortDramaProject", "ShortDramaProjectSummary"):
            self.assertIn("genre", schemas[project_schema]["required"])
            self.assertEqual(
                40, schemas[project_schema]["properties"]["genre"]["maxLength"]
            )
        self.assertIn("413", import_path["responses"])
        self._assert_openapi_sample(spec, import_schema, {
            "title": "Live action", "synopsis": "A complete live action story",
            "ratio": "16:9", "target_duration": 30, "shot_count": 6,
            "genre": "urban mystery", "visual_style": "cinematic",
            "source_requirement": "complete_story",
            "source_text": "Lin Xia: hello " * 10,
            "filename": "story.txt", "import_mode": "faithful",
            "content_type": "live_action", "character_contract": contract,
        })
        legacy_contract = [{
            **contract[0],
            "reference_views": ["front_full", "side_full", "front_half"],
        }]
        self._assert_openapi_sample(spec, import_schema, {
            "title": "Legacy live action",
            "synopsis": "A legacy client contract awaiting back-view confirmation",
            "ratio": "16:9", "target_duration": 30, "shot_count": 6,
            "visual_style": "cinematic", "source_text": "Lin Xia: hello",
            "filename": "legacy-story.txt", "import_mode": "faithful",
            "content_type": "live_action", "character_contract": legacy_contract,
        })
        role_variants = spec["paths"]["/api/gen/short-drama/project"]["put"][
            "requestBody"
        ]["content"]["application/json"]["schema"]["oneOf"]
        role_schema = next(
            item for item in role_variants
            if "character_contract" in item.get("required", [])
        )
        character = {
            "character_key": "character_1", "name": "Lin Xia",
            "identity_text": "main; female; 26; clerk", "personality": "calm",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "cinematic portrait; female; 26; oval; short; black; 165cm",
            "wardrobe_prompt": "white shirt; white; watch", "voice_key": None,
            "voice_settings": {},
        }
        added_contract = [*contract, {
            **contract[0], "character_key": "character_2", "name": "Zhou Ye",
            "role_type": "support",
        }]
        added_character = {
            **character, "character_key": "character_2", "name": "Zhou Ye",
            "identity_text": "support; female; 26; clerk",
        }
        for sample in (
            {"revision": 1, "characters": [character], "character_contract": contract},
            {"revision": 2, "characters": [character, added_character],
             "character_contract": added_contract},
            {"revision": 3, "characters": [added_character],
             "character_contract": [added_contract[1]]},
        ):
            self._assert_openapi_sample(spec, role_schema, sample)
        self.assertIn("ShortDramaCharacterContract", schemas)
        self.assertEqual(
            2000,
            schemas["ShortDramaCharacterContractItem"]["x-derivedIdentityMaxLength"],
        )

    def test_openapi_models_protected_character_reference_conflict(self) -> None:
        spec = load_json_strict(Path("docs/api/openapi.json"))
        schemas = spec["components"]["schemas"]
        conflict_schema = spec["paths"]["/api/gen/short-drama/project"]["put"][
            "responses"
        ]["409"]["content"]["application/json"]["schema"]

        self.assertEqual(
            ["revision_conflict", "job_already_applied"],
            schemas["RevisionConflict"]["properties"]["code"]["enum"],
        )
        self.assertEqual(
            [
                {"$ref": "#/components/schemas/RevisionConflict"},
                {"$ref": "#/components/schemas/CharacterReferenceProtectedConflict"},
            ],
            conflict_schema["oneOf"],
        )
        self._assert_openapi_sample(spec, conflict_schema, {
            "detail": "该角色已有付费或锁定的角色标准图，不能直接修改资料",
            "code": "character_reference_protected",
        })

    def test_openapi_covers_new_live_action_runtime_routes(self) -> None:
        spec = load_json_strict(Path("docs/api/openapi.json"))
        routes = {
            "/api/gen/short-drama/asset-graph/scenes": "get",
            "/api/gen/short-drama/asset-graph/scenes/reference": "post",
            "/api/gen/short-drama/asset-graph/scenes/lock": "post",
            "/api/gen/short-drama/projects/live-action/core-story": "post",
            "/api/gen/short-drama/projects/live-action/finalize": "post",
            "/api/gen/short-drama/autodraft/provider-version/select": "post",
            "/api/gen/short-drama/select-character-reference": "post",
        }

        for path, method in routes.items():
            with self.subTest(path=path):
                operation = spec["paths"][path][method]
                self.assertIn("200", operation["responses"])
                self.assertIn("401", operation["responses"])
                if method == "post":
                    self.assertIn("requestBody", operation)

    def test_openapi_candidate_adoption_is_server_owned(self) -> None:
        spec = load_json_strict(Path("docs/api/openapi.json"))
        operation = spec["paths"][
            "/api/gen/short-drama/refinement/candidates/adopt"
        ]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]

        self.assertEqual(
            [
                "project_id", "shot_key", "source_version_id",
                "replacement_provider_version_id",
            ],
            schema["required"],
        )
        self.assertNotIn("defer_reassembly", schema["properties"])
        self.assertTrue(next(
            item for item in operation["parameters"]
            if item["name"] == "Idempotency-Key"
        )["required"])

        reassembly = spec["paths"][
            "/api/gen/short-drama/refinement/candidates/reassemble"
        ]["post"]
        reassembly_schema = reassembly["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            ["project_id", "version_id"], reassembly_schema["required"]
        )

    def _assert_openapi_sample(self, spec, schema, value) -> None:
        if "$ref" in schema:
            target = spec
            for part in schema["$ref"].removeprefix("#/").split("/"):
                target = target[part]
            schema = target
        if value is None and schema.get("nullable"):
            return
        if "oneOf" in schema:
            matches = 0
            for option in schema["oneOf"]:
                try:
                    self._assert_openapi_sample(spec, option, value)
                except AssertionError:
                    continue
                matches += 1
            self.assertEqual(1, matches, "sample must match exactly one oneOf branch")
            return
        expected_type = schema.get("type")
        if not expected_type and ("properties" in schema or "required" in schema):
            expected_type = "object"
        if expected_type == "object":
            self.assertIsInstance(value, dict)
            required = set(schema.get("required", []))
            self.assertFalse(required - set(value), "sample is missing required properties")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                self.assertFalse(set(value) - set(properties), "sample has unknown properties")
            for key, item in value.items():
                if key in properties:
                    self._assert_openapi_sample(spec, properties[key], item)
        elif expected_type == "array":
            self.assertIsInstance(value, list)
            self.assertGreaterEqual(len(value), schema.get("minItems", 0))
            self.assertLessEqual(len(value), schema.get("maxItems", len(value)))
            for item in value:
                self._assert_openapi_sample(spec, schema.get("items", {}), item)
        elif expected_type == "string":
            self.assertIsInstance(value, str)
        elif expected_type == "integer":
            self.assertIs(type(value), int)
        if "enum" in schema:
            self.assertIn(value, schema["enum"])
