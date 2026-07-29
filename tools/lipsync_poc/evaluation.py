"""Aggregate redacted Provider reports into a repeatable Go/No-Go decision."""

import argparse
import json
import math
from pathlib import Path

from .paths import provider_slug
from .state import atomic_json, load_json


SUMMARY_VERSION = "1.1"
DEFAULT_MIN_SAMPLE_COUNT = 20


def _percentile(values, percentile):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return round(values[index], 3)


def _completed_review(report):
    review = report.get("human_review")
    if not isinstance(review, dict):
        return None
    if review.get("review_status") not in {"complete", "completed"}:
        return None
    return review


def _score(review, key):
    value = review.get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 5 else None


def _is_whole_sentence_offset(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "yes",
        "true",
        "obvious",
        "present",
        "明显",
        "有",
    }


def _provider_summary(provider, reports, minimum_sample_count):
    succeeded = [
        report for report in reports if report.get("status") == "succeeded"
    ]
    elapsed = [
        report.get("elapsed_ms")
        for report in succeeded
        if isinstance(report.get("elapsed_ms"), (int, float))
    ]
    reviews = [
        review
        for review in (_completed_review(report) for report in succeeded)
        if review is not None
    ]
    lip_scores = [
        score
        for score in (
            _score(review, "lip_sync_score_1_to_5")
            for review in reviews
        )
        if score is not None
    ]
    identity_scores = [
        score
        for score in (
            _score(review, "identity_score_1_to_5")
            for review in reviews
        )
        if score is not None
    ]
    visual_scores = [
        score
        for score in (
            _score(review, "visual_quality_score_1_to_5")
            for review in reviews
        )
        if score is not None
    ]
    av_offsets = [
        abs(float(review["av_offset_ms"]))
        for review in reviews
        if isinstance(review.get("av_offset_ms"), (int, float))
    ]
    usable = [
        review for review in reviews
        if (_score(review, "lip_sync_score_1_to_5") or 0) >= 3
        and (_score(review, "identity_score_1_to_5") or 0) >= 3
        and (_score(review, "visual_quality_score_1_to_5") or 0) >= 3
        and not _is_whole_sentence_offset(
            review.get("whole_sentence_offset")
        )
    ]
    costs = [
        report.get("estimated_cost_usd")
        for report in reports
        if isinstance(report.get("estimated_cost_usd"), (int, float))
    ]
    audio_violations = [
        report.get("sample_id")
        for report in succeeded
        if int(
            ((report.get("media") or {}).get("provider_output") or {}).get(
                "audio_stream_count"
            )
            or 0
        )
        != 0
    ]
    unresolved_billing = [
        report.get("sample_id")
        for report in reports
        if report.get("billing_status") == "requires_reconciliation"
    ]
    success_rate = len(succeeded) / len(reports) if reports else 0.0
    review_coverage = len(reviews) / len(succeeded) if succeeded else 0.0
    usable_rate = len(usable) / len(reviews) if reviews else None
    offset_rate = (
        sum(
            _is_whole_sentence_offset(
                review.get("whole_sentence_offset")
            )
            for review in reviews
        )
        / len(reviews)
        if reviews else None
    )
    identity_mean = (
        round(sum(identity_scores) / len(identity_scores), 3)
        if identity_scores else None
    )
    gates = {
        "sample_count_gte_minimum": (
            len(reports) >= minimum_sample_count
        ),
        "success_rate_gte_0_95": success_rate >= 0.95,
        "review_coverage_complete": review_coverage == 1.0,
        "usable_rate_gte_0_85": (
            usable_rate is not None and usable_rate >= 0.85
        ),
        "whole_sentence_offset_rate_lt_0_05": (
            offset_rate is not None and offset_rate < 0.05
        ),
        "av_offset_p95_lte_120ms": (
            bool(av_offsets) and _percentile(av_offsets, 0.95) <= 120
        ),
        "identity_mean_gte_4": (
            identity_mean is not None and identity_mean >= 4
        ),
        "output_is_silent": not audio_violations,
        "billing_is_reconciled": not unresolved_billing,
        "cost_is_configured": len(costs) == len(reports),
    }
    hard_fail = (
        bool(audio_violations)
        or bool(unresolved_billing)
        or (reports and success_rate < 0.95)
    )
    if hard_fail:
        decision = "no_go"
    elif all(gates.values()):
        decision = "go"
    else:
        decision = "conditional_go"
    return {
        "provider": provider,
        "decision": decision,
        "sample_count": len(reports),
        "minimum_sample_count": minimum_sample_count,
        "succeeded": len(succeeded),
        "success_rate": round(success_rate, 4),
        "elapsed_ms": {
            "p50": _percentile(elapsed, 0.50),
            "p95": _percentile(elapsed, 0.95),
        },
        "human_review": {
            "completed": len(reviews),
            "coverage": round(review_coverage, 4),
            "usable_rate": round(usable_rate, 4)
            if usable_rate is not None else None,
            "whole_sentence_offset_rate": round(offset_rate, 4)
            if offset_rate is not None else None,
            "av_offset_ms_p95": _percentile(av_offsets, 0.95),
            "lip_sync_mean": round(
                sum(lip_scores) / len(lip_scores), 3
            ) if lip_scores else None,
            "identity_mean": identity_mean,
            "visual_quality_mean": round(
                sum(visual_scores) / len(visual_scores), 3
            ) if visual_scores else None,
        },
        "cost": {
            "configured_reports": len(costs),
            "estimated_total_usd": round(sum(costs), 6)
            if costs else None,
        },
        "audio_violation_samples": audio_violations,
        "unresolved_billing_samples": unresolved_billing,
        "gates": gates,
    }


def build_summary(
    output_dir,
    providers,
    minimum_sample_count=DEFAULT_MIN_SAMPLE_COUNT,
):
    minimum_sample_count = int(minimum_sample_count)
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be positive")
    root = Path(output_dir)
    summaries = []
    for requested_provider in providers:
        provider = provider_slug(requested_provider)
        report_dir = root / provider / "reports"
        reports = []
        if report_dir.is_dir():
            for report_path in sorted(report_dir.glob("*.json")):
                report = load_json(report_path)
                if report and report.get("provider") == provider:
                    reports.append(report)
        summaries.append(
            _provider_summary(provider, reports, minimum_sample_count)
        )

    eligible = [
        item for item in summaries if item["decision"] == "go"
    ]
    default_provider = None
    if eligible:
        eligible.sort(key=lambda item: (
            -item["success_rate"],
            item["elapsed_ms"]["p95"]
            if item["elapsed_ms"]["p95"] is not None else float("inf"),
            item["cost"]["estimated_total_usd"]
            if item["cost"]["estimated_total_usd"] is not None
            else float("inf"),
            item["provider"],
        ))
        default_provider = eligible[0]["provider"]
    return {
        "summary_version": SUMMARY_VERSION,
        "providers": summaries,
        "default_provider": default_provider,
        "overall_decision": (
            "go"
            if default_provider
            else "no_go"
            if summaries and all(
                item["decision"] == "no_go" for item in summaries
            )
            else "conditional_go"
        ),
        "policy": {
            "success_rate_min": 0.95,
            "human_usable_rate_min": 0.85,
            "whole_sentence_offset_rate_max_exclusive": 0.05,
            "av_offset_ms_p95_max": 120,
            "identity_mean_min": 4,
            "minimum_sample_count": minimum_sample_count,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Summarize stage 0-B lip-sync Provider reports."
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["sync-labs", "fal-latentsync"],
    )
    parser.add_argument(
        "--minimum-sample-count",
        type=int,
        default=DEFAULT_MIN_SAMPLE_COUNT,
    )
    args = parser.parse_args(argv)
    summary = build_summary(
        args.output_dir,
        args.providers,
        minimum_sample_count=args.minimum_sample_count,
    )
    destination = Path(args.output_dir) / "evaluation-summary.json"
    atomic_json(destination, summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["overall_decision"] == "go" else 2


if __name__ == "__main__":
    raise SystemExit(main())
