"""Project-bound, private-workspace adapter for the frozen editorial template."""
import hashlib
import json
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

from . import editorial_contract as contract
from . import editorial_markup
from . import video_compose_media as media

_CLEANUP_FAILED = threading.Event()
_CLEANUP_BUDGET = 15

ASSET_HASHES = {
    "gsap.min.js": "c174bfce53a729418d57a8ad8625e7247c793a22fef8e2851e3cfa3de9cd8280",
    "SourceHanSerifSC-Heavy.otf": "d033af54f96530476faed924ab5d5e9e6ef0833495670fd57bab9a7758398048",
    "FONT-LICENSE.txt": "9ff5bb567e1b92c801fc1069e5fbf992ff8efccacb9db94e5959a5b3ba9bb903",
    "NotoSansSC-Regular.otf": "a2b93e6c2db05d6bbbf6f27d413ec73269735b7b679019c8a5aa9670ff0ffbf2",
    "ENGLISH-FONT-LICENSE.txt": "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
}


def _hash(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _regular(path):
    path = pathlib.Path(path).absolute()
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
           for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("模板输入和资源必须为受控普通文件")
    return path


def verify_assets(template_root):
    folder = pathlib.Path(template_root) / contract.TEMPLATE_ID
    for name, expected in ASSET_HASHES.items():
        if _hash(_regular(folder / name)) != expected:
            raise ValueError("口播网感模板资源校验失败：" + name)
    return folder


def project_input(project, body):
    """Bind a plan to the current transcript and confirmed EDL, never another video."""
    if set(body) - {"expected_revision", "template_id", "editorial_plan"}:
        raise ValueError("口播网感模板不接受旧模板参数")
    duration = int(project["edl"]["output_duration_ms"]) / 1000
    plan = contract.validate_plan(body.get("editorial_plan"), duration)
    if plan["transcript_hash"] != project.get("transcript_hash") or plan["edit_decision_version"] != project.get("edit_decision_version"):
        raise ValueError("字幕或剪辑版本已变化，请根据最新项目重新生成模板计划")
    # A word must survive entirely inside ONE keep range, not straddle a cut.
    words = []
    for word in project.get("words") or []:
        for kept_index, kept in enumerate(project["edl"]["keep_ranges"]):
            if kept["source_start_ms"] <= word["start_ms"] < word["end_ms"] <= kept["source_end_ms"]:
                words.append({**word, "range": kept_index,
                    "at": media.source_to_output_ms(word["start_ms"], project["edl"]) / 1000})
                break
    for punch in plan["keyword_punches"]:
        matches = [i for i, word in enumerate(words) if abs(word["at"] - punch["at"]) <= .08]
        proven = False
        for index in matches:
            spoken = ""
            previous_end = words[index]["start_ms"]
            for word in words[index:index + 10]:
                if (word["range"] != words[index]["range"] or word["start_ms"] - previous_end > 300
                        or word.get("timing_source") not in {"provider_word", "user_supplied"}):
                    break
                spoken += word["text"].strip()
                if spoken == punch["word"]:
                    proven = True
                    break
                if not punch["word"].startswith(spoken):
                    break
                previous_end = word["end_ms"]
        if not proven:
            raise ValueError("关键词缺少准确逐词时间证据；均分句子时间不可用于推近，请先校正转写")
    return {"template_id": contract.TEMPLATE_ID, "template_version": contract.TEMPLATE_VERSION,
            "duration_ms": int(project["edl"]["output_duration_ms"]), "editorial_plan": plan,
            "hook": {"line_1": plan["title"][0], "line_2": plan["title"][1]}}


def runtime_command():
    """No npx auto-download on a user request; deploy an isolated pinned package first."""
    if sys.platform.startswith("linux") and _CLEANUP_FAILED.is_set():
        raise ValueError("口播网感任务进程回收未完成，已暂停新任务；请联系运维检查后恢复")
    root = pathlib.Path(os.environ.get("VIDEO_COMPOSE_EDITORIAL_RUNTIME",
        "/opt/huangque/editorial-hyperframes-0.8.33/node_modules/hyperframes"))
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        if package.get("name") != "hyperframes" or package.get("version") != contract.HYPERFRAMES_VERSION:
            raise ValueError("口播网感模板需要锁定 HyperFrames 0.8.33")
        binary = package["bin"]
        entry = binary["hyperframes"] if isinstance(binary, dict) else binary
        target = (root / entry).resolve()
        target.relative_to(root.resolve())
        if not target.is_file():
            raise ValueError("模板运行程序缺失")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("口播网感模板独立渲染运行时尚未安装") from error
    node = os.environ.get("VIDEO_COMPOSE_EDITORIAL_NODE") or shutil.which("node")
    browser = os.environ.get("VIDEO_COMPOSE_EDITORIAL_BROWSER")
    if not node or not browser or not pathlib.Path(browser).is_file():
        raise ValueError("口播网感模板需要 Node 22+ 和显式配置的已验证浏览器")
    try:
        version = subprocess.check_output([node, "--version"], timeout=10).decode().strip()
        if int(version.lstrip("v").split(".")[0]) < 22:
            raise ValueError("Node version too old")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError("口播网感模板需要可运行的 Node 22+") from error
    return [node, str(target)], browser


def _run_owned_linux(command, workspace, environment, timeout):
    if _CLEANUP_FAILED.is_set():
        raise subprocess.SubprocessError("editorial cleanup incomplete; new work disabled until operator recovery")
    supervisor = pathlib.Path(__file__).with_name("editorial_process_supervisor.py")
    process = subprocess.Popen([sys.executable, "-B", str(supervisor), str(timeout), "--", *command],
        cwd=workspace, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout + _CLEANUP_BUDGET)
    except subprocess.TimeoutExpired as error:
        # Never kill the subreaper: doing so would orphan detached descendants.
        # Keep its pipes drained and reap it when the kernel permits cleanup.
        _CLEANUP_FAILED.set()
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        threading.Thread(target=process.communicate, daemon=True,
                         name="editorial-cleanup-reaper").start()
        logging.getLogger(__name__).critical(
            "Editorial cleanup budget exhausted; supervisor pid=%s retained; new editorial work disabled",
            process.pid)
        raise subprocess.SubprocessError("editorial cleanup incomplete; supervisor retained and new work disabled") from error
    if process.returncode == 124:
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    if process.returncode not in {0, 1, 125}:
        # A killed/crashed supervisor cannot certify ECHILD. Disable retries even
        # when it exited before the parent-side timeout budget was exhausted.
        _CLEANUP_FAILED.set()
        logging.getLogger(__name__).critical(
            "Editorial supervisor pid=%s exited unexpectedly (%s); new editorial work disabled",
            process.pid, process.returncode)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, 0, stdout, stderr)


def _run_owned(command, workspace, environment, timeout):
    """Own only this attempt's process tree, including Chrome/FFmpeg on timeout."""
    if sys.platform.startswith("linux"):
        return _run_owned_linux(command, workspace, environment, timeout)
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    process = subprocess.Popen(command, cwd=workspace, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=15, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def prepare_workspace(clean_video, payload, workspace, template_root):
    plan = contract.validate_plan(payload.get("editorial_plan"), payload["duration_ms"] / 1000)
    source = _regular(clean_video)
    report = media.probe_media(source)
    if not report["has_audio"] or report["duration_ms"] + 80 < payload["duration_ms"]:
        raise ValueError("口播原声缺失或实际视频短于模板时长")
    if abs(report["width"] / report["height"] - 9 / 16) > .015:
        raise ValueError("口播网感模板首版仅支持 9:16 原片，不自动裁掉横屏人物")
    assets = verify_assets(template_root)
    workspace = pathlib.Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "assets").mkdir()
    for name in ASSET_HASHES:
        shutil.copyfile(assets / name, workspace / "assets" / name)
    shutil.copyfile(source, workspace / "assets" / "source.mp4")
    job = {**plan, "duration": payload["duration_ms"] / 1000}
    markup = editorial_markup.make_html(job)
    (workspace / "index.html").write_bytes(markup.encode("utf-8"))
    (workspace / "package.json").write_text(json.dumps({"private": True, "type": "module",
        "scripts": {"check": "hyperframes check", "render": "hyperframes render"}}), encoding="utf-8")
    (workspace / "hyperframes.json").write_text(json.dumps({"paths": {"assets": "assets"},
        "authoringSkill": "general-video", "media": {"autoProxy": False}}), encoding="utf-8")
    manifest = {"template_id": contract.TEMPLATE_ID, "template_version": contract.TEMPLATE_VERSION,
        "hyperframes_version": contract.HYPERFRAMES_VERSION, "source_sha256": _hash(source),
        "html_sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(), "assets": ASSET_HASHES,
        "plan_sha256": hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest(),
        "width": 720, "height": 1280, "fps": 30, "caption_count": len(plan["captions"])}
    (workspace / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def render(clean_video, payload, output_path, template_root, timeout=None):
    command, browser = runtime_command()
    output_path = pathlib.Path(output_path).absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Stage output; a failed attempt must never replace a previously accepted MP4.
    with tempfile.TemporaryDirectory(prefix="editorial-", dir=output_path.parent) as directory:
        root = pathlib.Path(directory)
        workspace = root / "project"
        manifest = prepare_workspace(clean_video, payload, workspace, template_root)
        staged = root / "render.mp4"
        environment = {**os.environ, "HYPERFRAMES_BROWSER_PATH": browser,
            "HYPERFRAMES_SKIP_SKILLS": "1", "HYPERFRAMES_NO_TELEMETRY": "1",
            "DO_NOT_TRACK": "1", "PRODUCER_LOW_MEMORY_MODE": "1"}
        try:
            for args in (["check", str(workspace)],
                         ["render", str(workspace), "--output", str(staged), "--fps", "30",
                          "--quality", "high", "--workers", "1", "--low-memory-mode", "--strict", "--quiet"]):
                _run_owned(command + args, workspace, environment,
                    timeout or max(900, int(payload["duration_ms"] / 1000 * 20)))
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("口播网感模板检查或渲染失败；请检查固定运行时、字体和字幕布局") from error
        report = media.probe_media(staged)
        if (report["video_codec"] != "h264" or report["audio_codec"] != "aac"
                or (report["width"], report["height"]) != (720, 1280)
                or abs(report["duration_ms"] - payload["duration_ms"]) > 150):
            raise ValueError("口播网感成片编码、画幅或时长未通过校验")
        os.replace(staged, output_path)
    return {"template_id": contract.TEMPLATE_ID, "template_version": contract.TEMPLATE_VERSION,
            "output": report, "build_manifest": manifest, "render_log": "ok"}
