"""Command-line entry point for the stage 0-A/0-B lip-sync PoC."""

import argparse
import json
from pathlib import Path

from .adapters.mock import MockLipsyncProvider
from .adapters.fal_latentsync import FalLatentSyncProvider
from .adapters.sync_labs import SyncLabsProvider
from .manifest import load_manifest
from .runner import PocRunError, PocRunner


PROVIDERS = {
    "fal-latentsync": FalLatentSyncProvider,
    "mock": MockLipsyncProvider,
    "sync-labs": SyncLabsProvider,
}


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
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument(
        "--resume",
        action="store_true",
        help="Resume polling a persisted provider job without creating a new one.",
    )
    recovery.add_argument(
        "--refetch",
        action="store_true",
        help="Download a completed provider result using the persisted job ID.",
    )
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
                resume=args.resume,
                refetch=args.refetch,
            )
        except PocRunError as error:
            failure = {
                "sample_id": sample.sample_id,
                "code": error.code,
                "message": str(error),
            }
            if error.report:
                failure.update({
                    "provider_job_id": error.report.get(
                        "provider_job_id"
                    ),
                    "billing_status": error.report.get(
                        "billing_status"
                    ),
                    "artifact_namespace": error.report.get(
                        "artifact_namespace"
                    ),
                    "report_file": error.report.get("report_file"),
                    "recovery": error.report.get("recovery"),
                })
            failures.append(failure)
    print(json.dumps({
        "provider": args.provider,
        "total": len(samples),
        "succeeded": len(samples) - len(failures),
        "failed": failures,
    }, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
