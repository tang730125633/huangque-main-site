"""黄雀 CLI（hq）的薄封装：subprocess 调用 + JSON 解析。

只做「把参数传给 CLI、把结果转成 dict」，不做任何业务判断——
什么时候调、传什么，完全交给主 Agent。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading

from . import config

_TIMEOUT = int(os.environ.get("HQ_TIMEOUT", "180"))

# 全局并发闸：hq CLI 子进程全局限 5 并行（对齐主站上限），
# 主 Agent 轮次已并发化，多轮同时跑重活时不再无限制地轰炸 CLI 服务。
_HQ_SEMAPHORE = threading.Semaphore(int(os.environ.get("HQ_MAX_PARALLEL", "5")))


def hq_semaphore() -> threading.Semaphore:
    """返回全局并发闸（livecaps 等直连 subprocess 的模块也用它对齐上限）。"""
    return _HQ_SEMAPHORE


def _subprocess_run(cmd: list) -> subprocess.CompletedProcess:
    with _HQ_SEMAPHORE:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)


def _bin() -> str:
    return config.HQ_BIN


def run(
    capability: str,
    inputs: dict | None = None,
    confirm: bool = False,
    quote_token: str | None = None,
    expected_cost=None,
    file_path: str | None = None,
    output: str | None = None,
):
    """运行 `hq run <capability> [--input @file] [--file <path>] [--output <path>]
    [--confirm] [--quote-token <token>] [--expected-cost <cost>] --json`。

    - inputs 会被写入临时文件后以 `--input @file` 传入；
    - confirm=True 时追加 `--confirm`（写入/提交类操作）；
    - 付费两段式：quote_token 由上一次不带 --confirm 的报价返回，确认时原样传入；
    - file_path 用于上传类能力（如 director-breakdown-upload）的本地文件；
    - output 用于落盘类能力（如 dl 下载成品）的绝对输出路径。
    """
    cmd = [_bin(), "run", capability]
    tmp_path = None
    try:
        # 空 inputs（None 或 {}）不传 --input：上传类能力（--file 流式上传）
        # 明确拒绝 --input（"upload capabilities do not accept --input"）。
        if inputs:
            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="hq_input_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(inputs, f, ensure_ascii=False)
            cmd += ["--input", "@" + tmp_path]
        if file_path:
            cmd += ["--file", file_path]
        if output:
            cmd += ["--output", output]
        if confirm:
            cmd += ["--confirm"]
        if quote_token:
            cmd += ["--quote-token", quote_token]
        if expected_cost is not None:
            cmd += ["--expected-cost", str(expected_cost)]
        cmd += ["--json"]

        proc = _subprocess_run(cmd)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        # 成功结果在 stdout；错误结果（JSON）在 stderr。
        raw = stdout or stderr
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw_output": raw[:2000]}
        return {
            "exit_code": proc.returncode,
            "data": data,
            "stderr": stderr[:800],
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "data": {"error": "timeout"}, "stderr": ""}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def status():
    """`hq status --json`"""
    try:
        proc = _subprocess_run([_bin(), "status", "--json"])
        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw_output": raw[:2000]}
        return {"exit_code": proc.returncode, "data": data, "stderr": (proc.stderr or "")[:800]}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "data": {"error": "timeout"}, "stderr": ""}


def describe(capability: str):
    """`hq describe ID --json`"""
    try:
        proc = _subprocess_run([_bin(), "describe", capability, "--json"])
        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw_output": raw[:2000]}
        return {"exit_code": proc.returncode, "data": data, "stderr": (proc.stderr or "")[:800]}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "data": {"error": "timeout"}, "stderr": ""}
