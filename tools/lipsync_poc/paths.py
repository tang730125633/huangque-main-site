"""Safe provider-scoped artifact paths for lip-sync evaluations."""

import re
from dataclasses import dataclass
from pathlib import Path


_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def provider_slug(value):
    raw = str(value or "").strip()
    slug = raw.lower()
    if raw != slug:
        raise ValueError("provider name must be lowercase")
    if not _SAFE_PROVIDER.fullmatch(slug):
        raise ValueError(
            "provider name must contain only lowercase letters, numbers, "
            "dots, underscores, or hyphens"
        )
    return slug


@dataclass(frozen=True)
class ArtifactPaths:
    root: Path
    provider: str
    state: Path
    media: Path
    report: Path


def artifact_paths(output_dir, provider, sample_id):
    slug = provider_slug(provider)
    root = Path(output_dir) / slug
    filename = str(sample_id)
    return ArtifactPaths(
        root=root,
        provider=slug,
        state=root / "state" / f"{filename}.json",
        media=root / "media" / f"{filename}.mp4",
        report=root / "reports" / f"{filename}.json",
    )
