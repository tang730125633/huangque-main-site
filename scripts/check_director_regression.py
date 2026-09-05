"""Run all Director checks on Base and Head; fail on ANY regression.

Known baseline failures remain printed and recorded, never skipped or patched.
This is a scoped Base/Head comparison, not a claim that the whole repo is green.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

MODULES = (
    "tests.test_director_agent_confirmation", "tests.test_director_agent_explicit_confirmation",
    "tests.test_director_cli", "tests.test_director_cli_contract", "tests.test_director_workflows",
)
FRONTEND = ("tests/test_director_agent.js", "tests/test_director_agent_confirmation.js")


def compare(base, head):
    problems = []
    for label, report in (("Base", base), ("Head", head)):
        if any("unittest.loader._FailedTest" in name for name in report["tests"]):
            problems.append(label + " test module failed to import; baseline exemption is not allowed")
    if not set(base["tests"]) <= set(head["tests"]):
        problems.append("Base tests were removed")
    if head["count"] < base["count"]:
        problems.append("Discovered test count decreased")
    if not set(head["skips"]) <= set(base["skips"]):
        problems.append("New skips detected")
    for name, detail in head["failures"].items():
        if base["failures"].get(name) != detail:
            problems.append("Head-only or changed failure: " + name)
    return problems


def worker(root, with_new):
    os.chdir(root)
    sys.path.insert(0, str(root))
    modules = list(MODULES)
    if with_new:
        modules.append("tests.test_director_conversation")
    found = []

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            found.append(test.id())
            super().startTest(test)

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(stream=captured, resultclass=Result, verbosity=2).run(suite)
    failures = {test.id(): detail for test, detail in result.failures + result.errors}
    for file in FRONTEND:
        proc = subprocess.run([os.environ.get("DIRECTOR_TEST_NODE", "node"), file], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        found.append(file)
        if proc.returncode:
            failures[file] = proc.stdout + proc.stderr
    # Ignore checkout and randomly allocated temp paths, not assertion content.
    def normalize(detail):
        detail = detail.replace(str(root), "<repo>").replace(str(root).replace("\\", "/"), "<repo>")
        return re.sub(r"tmp[a-z0-9_]{8}(?=[/\\])", "<temp>", detail)
    return {"tests": found, "count": result.testsRun + len(FRONTEND),
            "skips": [test.id() for test, _ in result.skipped],
            "failures": {name: normalize(detail) for name, detail in failures.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--with-new", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker.resolve(), args.with_new), ensure_ascii=False))
        return 0
    if not args.base:
        parser.error("--base is required")
    root = Path(__file__).resolve().parents[1]
    script = Path(__file__).resolve()
    base_sha = subprocess.check_output(["git", "rev-parse", "--verify", args.base + "^{commit}"], cwd=root, text=True).strip()
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    def run_worker(tree, new=False):
        command = [sys.executable, str(script), "--worker", str(tree)]
        if new:
            command.append("--with-new")
        proc = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=240)
        if proc.returncode:
            raise RuntimeError("Test worker failed: " + proc.stderr)
        return json.loads(proc.stdout)
    with tempfile.TemporaryDirectory(prefix="director-regression-") as temporary:
        tree = Path(temporary).resolve() / "baseline"
        subprocess.run(["git", "worktree", "add", "--detach", str(tree), base_sha], cwd=root, check=True, capture_output=True)
        try:
            base = run_worker(tree)
            head = run_worker(root, True)
        finally:
            # Exact task-created checkout only; no user working tree is removed.
            assert tree.parent == Path(temporary).resolve() and tree.name == "baseline"
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=root, check=True, capture_output=True)
    problems = compare(base, head)
    report = {"base_sha": base_sha, "head_sha": head_sha, "base": base, "head": head,
              "regressions": problems, "all_tests_green": not head["failures"] and not head["skips"]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("PASS: no Head-only regressions; baseline failures remain listed" if not problems else "FAIL: regression detected")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
