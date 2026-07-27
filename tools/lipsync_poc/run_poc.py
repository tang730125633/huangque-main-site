"""Command-line entry point for the offline PR 0-A PoC framework."""

import argparse
import json
from pathlib import Path

from .adapters.mock import MockLipsyncProvider
from .manifest import load_manifest
from .runner import PocRunError, PocRunner


PROVIDERS = {"mock": MockLipsyncProvider}


def build_parser():
    parser = argparse.ArgumentParser(
        description="Validate or run the short-drama lip-sync PoC manifest."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--assets-root", required=True)
    parser.add_argument("--output-dir", default=".local-content-out/lipsync-poc")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="mock")
    parser.add_argument("--sample-id")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    samples = load_manifest(args.manifest, args.assets_root)
    if args.sample_id:
        samples = [sample for sample in samples if sample.sample_id == args.sample_id]
        if not samples:
            raise SystemExit("sample_id was not found in the manifest")
    if args.validate_only:
        print(json.dumps({
            "validated": len(samples),
            "sample_ids": [sample.sample_id for sample in samples],
        }, ensure_ascii=False))
        return 0

    provider = PROVIDERS[args.provider]()
    runner = PocRunner(provider)
    failures = []
    for sample in samples:
        try:
            runner.run(
                sample,
                Path(args.output_dir),
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
        except PocRunError as error:
            failures.append({
                "sample_id": sample.sample_id,
                "code": error.code,
                "message": str(error),
            })
    print(json.dumps({
        "provider": args.provider,
        "total": len(samples),
        "succeeded": len(samples) - len(failures),
        "failed": failures,
    }, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
