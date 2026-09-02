"""Install the canonical Huangque Agent Skill into supported harnesses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__


SKILL_VERSION = "0.3.0"
SKILL_COMMIT = "da65252638da03634d591d8a6bbc2901cc7b3522"
MANIFEST_SHA256 = "be1482f93b4d89d5c26aa065996b42061eb117f219af2f03219f6eed5d9a1974"
MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    "tang730125633/huangque-agent-skill/%s/manifest.json" % SKILL_COMMIT
)
RAW_ROOT = (
    "https://raw.githubusercontent.com/"
    "tang730125633/huangque-agent-skill"
)
SKILL_NAME = "use-huangque-cli"
SKILL_PREFIX = "skills/%s/" % SKILL_NAME
EXPECTED_FILES = {
    SKILL_PREFIX + "SKILL.md",
    SKILL_PREFIX + "agents/openai.yaml",
}
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SKILL_FILE_BYTES = 256 * 1024
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class SkillInstallError(Exception):
    def __init__(self, error, message, details=None):
        super().__init__(message)
        self.error = str(error)
        self.message = str(message)
        self.details = details or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url, max_bytes):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "hq-cli/%s" % __version__})
    try:
        with opener.open(request, timeout=15) as response:
            if response.getcode() != 200:
                raise SkillInstallError("skill_download_error", "download returned HTTP %s" % response.getcode())
            raw = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise SkillInstallError("skill_download_error", "cannot download Huangque Agent Skill: %s" % exc)
    if len(raw) > max_bytes:
        raise SkillInstallError("skill_download_error", "downloaded Skill file is too large")
    return raw


def _version_tuple(value):
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise SkillInstallError("skill_manifest_error", "invalid semantic version in Skill manifest")
    return tuple(int(part) for part in value.split("."))


def _validated_manifest(fetch, expected_sha256):
    try:
        raw = fetch(MANIFEST_URL, MAX_MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise SkillInstallError("skill_hash_error", "Skill manifest SHA-256 verification failed")
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillInstallError("skill_manifest_error", "invalid Skill manifest: %s" % exc)
    if not isinstance(manifest, dict) or manifest.get("schema") != "huangque.agent-skill/v1":
        raise SkillInstallError("skill_manifest_error", "unsupported Skill manifest schema")
    skill = manifest.get("skill") or {}
    version = skill.get("version")
    if skill.get("name") != SKILL_NAME or version != SKILL_VERSION:
        raise SkillInstallError("skill_manifest_error", "unexpected Skill identity")
    _version_tuple(version)
    if manifest.get("source_ref") != "v" + version:
        raise SkillInstallError("skill_manifest_error", "Skill source ref must match its version")
    cli = manifest.get("cli") or {}
    minimum = cli.get("minimum")
    if _version_tuple(__version__) < _version_tuple(minimum):
        raise SkillInstallError(
            "skill_cli_incompatible",
            "Huangque Agent Skill requires hq %s or newer" % minimum,
            {"installed_cli_version": __version__, "minimum_cli_version": minimum},
        )
    files = manifest.get("files")
    if (not isinstance(files, list) or len(files) != len(EXPECTED_FILES)
            or not all(isinstance(item, dict) for item in files)
            or {item.get("path") for item in files} != EXPECTED_FILES):
        raise SkillInstallError("skill_manifest_error", "unexpected Skill file set")
    for item in files:
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SkillInstallError("skill_manifest_error", "invalid Skill file hash")
    adapters = manifest.get("adapters")
    if not isinstance(adapters, dict) or set(adapters) != {"deepseek", "codex", "openclaw", "pi", "mcp"}:
        raise SkillInstallError("skill_manifest_error", "unexpected Skill adapter set")
    mcp = adapters.get("mcp") or {}
    if mcp.get("command") != "hq" or mcp.get("args") != ["mcp"]:
        raise SkillInstallError("skill_manifest_error", "unexpected MCP command")
    _version_tuple(mcp.get("minimum_cli"))
    return manifest


def _target_path(target, home=None):
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    if target == "codex" and home is None and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser().resolve() / "skills" / SKILL_NAME
    roots = {
        "deepseek": home_path / ".dsh" / "skills",
        "codex": home_path / ".codex" / "skills",
        "openclaw": home_path / ".openclaw" / "skills",
        "pi": home_path / ".pi" / "agent" / "skills",
    }
    if target not in roots:
        raise SkillInstallError("skill_target_error", "unsupported Skill target: %s" % target)
    return roots[target] / SKILL_NAME


def _installed_state(destination, manifest):
    marker = destination / ".huangque-skill.json"
    try:
        installed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "unmanaged"
    if installed.get("schema") != "huangque.skill-install/v1" or installed.get("name") != SKILL_NAME:
        return "unmanaged"
    expected = {item["path"]: item["sha256"] for item in installed.get("files") or [] if isinstance(item, dict)}
    if set(expected) != EXPECTED_FILES:
        return "modified"
    for source, digest in expected.items():
        if not source.startswith(SKILL_PREFIX) or source not in EXPECTED_FILES:
            return "modified"
        path = destination / source[len(SKILL_PREFIX):]
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "modified"
        if actual != digest:
            return "modified"
    if not expected:
        return "modified"
    return "current" if installed.get("version") == manifest["skill"]["version"] else "managed"


def _stage_skill(destination, manifest, fetch):
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".%s-stage-" % SKILL_NAME, dir=str(destination.parent)))
    try:
        for item in manifest["files"]:
            source = item["path"]
            url = "%s/%s/%s" % (RAW_ROOT, SKILL_COMMIT, source)
            raw = fetch(url, MAX_SKILL_FILE_BYTES)
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise SkillInstallError("skill_hash_error", "Skill file SHA-256 verification failed", {"file": source})
            relative = source[len(SKILL_PREFIX):]
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        marker = {
            "schema": "huangque.skill-install/v1",
            "name": SKILL_NAME,
            "version": manifest["skill"]["version"],
            "source_ref": manifest["source_ref"],
            "files": manifest["files"],
        }
        (stage / ".huangque-skill.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _backup_path(destination):
    while True:
        candidate = destination.with_name(destination.name + ".backup-" + uuid.uuid4().hex[:8])
        if not candidate.exists() and not candidate.is_symlink():
            return candidate


def install_skill(target, replace=False, home=None, fetch=_download, _manifest_sha256=MANIFEST_SHA256):
    manifest = _validated_manifest(fetch, _manifest_sha256)
    version = manifest["skill"]["version"]
    if target == "mcp":
        minimum = manifest["adapters"]["mcp"]["minimum_cli"]
        if _version_tuple(__version__) < _version_tuple(minimum):
            raise SkillInstallError(
                "skill_cli_incompatible",
                "the Huangque MCP entry requires hq %s or newer" % minimum,
                {"installed_cli_version": __version__, "minimum_cli_version": minimum},
            )
        return {
            "target": "mcp",
            "status": "available",
            "skill_version": version,
            "server": {"command": "hq", "args": ["mcp"]},
        }

    destination = _target_path(target, home=home)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SkillInstallError("skill_destination_error", "refusing to replace non-directory Skill destination")
    state = _installed_state(destination, manifest) if destination.exists() else "missing"
    if state == "current":
        return {
            "target": target,
            "status": "current",
            "skill_version": version,
            "destination": str(destination),
        }
    if state in {"unmanaged", "modified"} and not replace:
        raise SkillInstallError(
            "skill_replace_required",
            "existing Skill is not safely managed; review it and re-run with --replace",
            {"destination": str(destination), "state": state},
        )

    stage = _stage_skill(destination, manifest, fetch)
    old = None
    keep_old = state in {"unmanaged", "modified"}
    try:
        if destination.exists():
            old = _backup_path(destination) if keep_old else destination.with_name(
                ".%s-old-%s" % (SKILL_NAME, uuid.uuid4().hex)
            )
            os.replace(destination, old)
        os.replace(stage, destination)
    except Exception:
        if old is not None and old.exists() and not destination.exists():
            os.replace(old, destination)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if old is not None and not keep_old:
        shutil.rmtree(old, ignore_errors=True)
    result = {
        "target": target,
        "status": "installed" if state == "missing" else "updated",
        "skill_version": version,
        "destination": str(destination),
    }
    if old is not None and keep_old:
        result["backup"] = str(old)
    return result
