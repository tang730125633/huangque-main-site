"""Exact seven-file main Agent v4 release; default is verification only.

Build only from a clean GitHub main commit. The bundle never contains env,
sessions, generated media, or old Hermes IP12 files. Apply preserves all seven
original files and rolls them back on install/restart/health/static failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("deploy/hq-ip-agent-ux.json")
TARGETS = {"app.py", "agent/v4/main_agent.py", "agent/v4/subagent.py",
           "agent/v4/state.py", "agent/v4/delivery.py", "static/v4.js", "static/style.css"}
LIVE = Path("/home/ubuntu/hq-ip-agent")
SERVICE = "hq-ip-agent"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(manifest):
    rows = manifest["files"]
    if len(rows) != len(TARGETS) or {r["target"] for r in rows} != TARGETS:
        raise ValueError("release must contain exactly the seven agreed files")
    if manifest["target_root"] != "/home/ubuntu/hq-ip-agent" or manifest["service"] != SERVICE:
        raise ValueError("wrong production target")
    if manifest["repository"] != "tang730125633/huangque-main-site":
        raise ValueError("wrong GitHub repository")
    for row in rows:
        expected = ("site/workbench/hq-ip-agent/" if row["target"].startswith("static/")
                    else "server/hq_ip_agent/") + row["target"]
        if row["source"] != expected:
            raise ValueError("source mapping mismatch")
        if any(not re.fullmatch(r"[a-f0-9]{64}", row[k]) for k in ("before", "after")):
            raise ValueError("invalid hash")
    return rows


def checked_path(root, name):
    path = root / name
    if path.resolve() != root.resolve() / name or not path.is_file():
        raise ValueError("missing or redirected file: " + name)
    return path


def preflight(bundle, live, manifest):
    for row in validate(manifest):
        source = checked_path(bundle, row["target"])
        target = checked_path(live, row["target"])
        if digest(source) != row["after"] or digest(target) != row["before"]:
            raise ValueError("hash mismatch: " + row["target"])
        if source.suffix == ".py":
            compile(source.read_bytes(), row["target"], "exec")


def atomic_copy(source, target):
    meta = target.stat()
    fd, name = tempfile.mkstemp(prefix=".hq-ux-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, stat.S_IMODE(meta.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, meta.st_uid, meta.st_gid)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def install(bundle, live, backup, manifest, control, check_health):
    """Injected paths/callbacks support offline fault-injection tests."""
    preflight(bundle, live, manifest)
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    rows = manifest["files"]
    for row in rows:
        saved = backup / row["target"]
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live / row["target"], saved)
        if digest(saved) != row["before"]:
            raise ValueError("backup mismatch")
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    try:
        control("stop")
        preflight(bundle, live, manifest)
        for row in rows:
            atomic_copy(bundle / row["target"], live / row["target"])
        for row in rows:
            if digest(live / row["target"]) != row["after"]:
                raise RuntimeError("installed hash mismatch")
        control("start")
        check_health("after")
    except BaseException as original:
        try:
            control("stop")
            for row in rows:
                atomic_copy(backup / row["target"], live / row["target"])
                if digest(live / row["target"]) != row["before"]:
                    raise RuntimeError("rollback hash mismatch")
            control("start")
            check_health("before")
        except BaseException as rollback_error:
            raise RuntimeError("ROLLBACK FAILED; retained backup: " + str(backup)) from rollback_error
        raise RuntimeError("release failed; original files restored: " + type(original).__name__) from original


def git(*args):
    return subprocess.check_output(["git", "--no-pager", *args], cwd=ROOT)


def build(destination, commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("exact commit required")
    if git("rev-parse", "HEAD").decode().strip() != commit or git("status", "--porcelain").strip():
        raise ValueError("checkout must be clean and at exact release commit")
    if git("rev-parse", "origin/main").decode().strip() != commit:
        raise ValueError("refresh origin/main and build from its exact merged commit")
    manifest = json.loads(git("show", commit + ":" + MANIFEST.as_posix()))
    rows = validate(manifest)
    manifest["github_commit"] = commit
    # Read only tracked blobs, never copy working-directory files or ignored data.
    payloads = {r["target"]: git("show", commit + ":" + r["source"]) for r in rows}
    for row in rows:
        if hashlib.sha256(payloads[row["target"]]).hexdigest() != row["after"]:
            raise ValueError("Git blob does not match release manifest")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in payloads.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (destination / "release.py").write_bytes(git("show", commit + ":scripts/hq_ip_agent_release.py"))
    print(json.dumps({"bundle": str(destination), "github_commit": commit, "files": len(rows)}))


def live_health(manifest, which):
    last_error = None
    for attempt in range(8):
        try:
            subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=True)
            for base in ("http://127.0.0.1:8000", "https://huangquechuanmei.com/workbench/ip12"):
                with urlopen(base + "/api/health", timeout=10) as response:
                    health = json.load(response)
                if health.get("hq_status", {}).get("ok") is not True:
                    raise RuntimeError("CLI health is not ready")
                with urlopen(base + "/v4", timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError("page is not ready")
                for row in manifest["files"]:
                    if row["target"].startswith("static/"):
                        with urlopen(base + "/" + row["target"], timeout=10) as response:
                            if hashlib.sha256(response.read()).hexdigest() != row[which]:
                                raise RuntimeError("served static hash mismatch")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError("health/static verification failed") from last_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.build:
        build(args.build, args.commit or "")
        return
    if not args.bundle:
        manifest = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
        for row in validate(manifest):
            if digest(ROOT / row["source"]) != row["after"]:
                raise ValueError("repository manifest mismatch: " + row["source"])
        print("seven-file repository manifest verified")
        return
    manifest = json.loads((args.bundle / "manifest.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit or "") or manifest.get("github_commit") != args.commit:
        raise ValueError("bundle commit mismatch")
    preflight(args.bundle, LIVE, manifest)
    current = subprocess.check_output(["git", "-c", "safe.directory=" + str(LIVE), "-C", str(LIVE), "rev-parse", "HEAD"]).decode().strip()
    if current != manifest["production_base"]:
        raise ValueError("production baseline commit drifted")
    workdir = subprocess.check_output(["systemctl", "show", SERVICE, "-p", "WorkingDirectory", "--value"]).decode().strip()
    if workdir != str(LIVE):
        raise ValueError("service workdir mismatch")
    live_health(manifest, "before")
    if not args.apply:
        print("production preflight passed; no writes or restarts")
        return
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("apply requires the authorized production operator")
    parent = Path("/home/ubuntu/release-backup")
    parent.mkdir(mode=0o700, exist_ok=True)
    backup = parent / ("hq-ip-agent-ux-" + args.commit[:12] + "-" + str(time.time_ns()))
    def control(action):
        subprocess.run(["systemctl", action, SERVICE], check=True, timeout=30)
    install(args.bundle, LIVE, backup, manifest, control, lambda phase: live_health(manifest, phase))
    print(json.dumps({"deployed": True, "github_commit": args.commit, "backup": str(backup),
                      "service": SERVICE, "files": len(manifest["files"]), "health": "passed"}))


if __name__ == "__main__":
    main()
