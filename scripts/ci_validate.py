#!/usr/bin/env python3
"""黄雀主站 CI 的零依赖静态检查。

检查已跟踪文件是否触碰安全红线，并确认 HTML 中的本地链接和资源存在。
不读取、不上传、不调用任何生产数据或外部服务。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "site"

FORBIDDEN_PARTS = {"browser_data", "data"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".key", ".pem"}
FORBIDDEN_NAMES = {".env", "jobs.db"}
FORBIDDEN_PREFIXES = ("secrets",)

CHECKED_ATTRIBUTES = {"href", "src", "poster"}
IGNORED_SCHEMES = {"data", "http", "https", "mailto", "tel", "blob", "javascript"}
IGNORED_SERVER_PREFIXES = ("/api/", "/admin/", "/health")


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_strict(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=reject_duplicate_json_keys,
    )


def load_json_strict(path: Path) -> object:
    return parse_json_strict(path.read_text(encoding="utf-8"))


def check_json_files(files: list[PurePosixPath]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix.lower() != ".json":
            continue
        try:
            load_json_strict(ROOT / path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"JSON 无法严格解析：{path}: {exc}")
    return errors


def tracked_files() -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        PurePosixPath(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def check_redlines(files: list[PurePosixPath]) -> list[str]:
    errors: list[str] = []
    for path in files:
        parts = set(path.parts)
        name = path.name.lower()
        suffix = path.suffix.lower()
        if parts & FORBIDDEN_PARTS:
            errors.append(f"安全红线：禁止跟踪目录中的文件 {path}")
        elif name in FORBIDDEN_NAMES or name.startswith(".env."):
            errors.append(f"安全红线：禁止跟踪凭据/数据库文件 {path}")
        elif suffix in FORBIDDEN_SUFFIXES:
            errors.append(f"安全红线：禁止跟踪敏感后缀文件 {path}")
        elif name.startswith(FORBIDDEN_PREFIXES):
            errors.append(f"安全红线：禁止跟踪密钥文件 {path}")
    errors.extend(check_systemd_secrets(files))
    return errors


# systemd 单元里的 Environment= 是明文的，且 /etc/systemd/system 默认 644（谁都能读）。
# 线上 huangque-content.service.d/doubao.conf 就这么放着 DOUBAO_TOKEN（已收紧为 600）。
# 密钥应当走 EnvironmentFile 指向 600 的 env 文件，那种文件本来就不进 git。
_SECRET_KEY_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY", "_KEY", "COOKIE")
_ENV_LINE = re.compile(r'^\s*Environment\s*=\s*"?([A-Za-z0-9_]+)=(.*?)"?\s*$')


def check_systemd_secrets(files: list[PurePosixPath]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if not str(path).startswith("deploy/systemd/") or path.suffix == ".example":
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = _ENV_LINE.match(line)
            if not m:
                continue
            key, value = m.group(1).upper(), m.group(2).strip()
            if not value or value.startswith("<"):
                continue                      # 占位符，例如 <从 content.env 取>
            if any(h in key for h in _SECRET_KEY_HINTS):
                errors.append(
                    f"安全红线：systemd 单元里有明文密钥 {path}:{lineno} 的 {m.group(1)}"
                    f"（改用 EnvironmentFile 指向 600 的 env 文件；只提交 .example）"
                )
    return errors


class ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in CHECKED_ATTRIBUTES and value:
                self.references.append((self.getpos()[0], name, value.strip()))


def is_dynamic_or_external(reference: str) -> bool:
    if not reference or reference.startswith("#"):
        return True
    if any(token in reference for token in ("${", "{{", "<%")):
        return True
    parsed = urlsplit(reference)
    return bool(parsed.netloc or parsed.scheme.lower() in IGNORED_SCHEMES)


def candidate_paths(html_file: Path, reference: str) -> list[Path]:
    path_text = unquote(urlsplit(reference).path)
    if not path_text:
        return []
    if path_text.startswith(IGNORED_SERVER_PREFIXES):
        return []

    if path_text == "/":
        base = STATIC_ROOT / "index.html"
    elif path_text.startswith("/"):
        base = STATIC_ROOT / path_text.lstrip("/")
    else:
        base = html_file.parent / path_text

    candidates = [base]
    if not base.suffix:
        candidates.extend((base.with_suffix(".html"), base / "index.html"))
    return candidates


def check_html_references(files: list[PurePosixPath]) -> list[str]:
    errors: list[str] = []
    html_files = [
        ROOT / path
        for path in files
        if path.suffix.lower() == ".html" and path.parts[0] in {"site", "server"}
    ]

    for html_file in html_files:
        parser = ReferenceParser()
        try:
            parser.feed(html_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            errors.append(f"HTML 无法读取：{html_file.relative_to(ROOT)}: {exc}")
            continue

        for line, attribute, reference in parser.references:
            if is_dynamic_or_external(reference):
                continue
            candidates = candidate_paths(html_file, reference)
            if candidates and not any(path.resolve().exists() for path in candidates):
                rel = html_file.relative_to(ROOT)
                errors.append(f"HTML 资源丢失：{rel}:{line} {attribute}=\"{reference}\"")
    return errors


def main() -> int:
    files = tracked_files()
    errors = check_redlines(files)
    errors.extend(check_json_files(files))
    errors.extend(check_html_references(files))

    if errors:
        print("CI 静态检查失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    html_count = sum(
        path.suffix.lower() == ".html" and path.parts[0] in {"site", "server"}
        for path in files
    )
    print(f"CI 静态检查通过：{len(files)} 个跟踪文件，{html_count} 个 HTML 页面。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
