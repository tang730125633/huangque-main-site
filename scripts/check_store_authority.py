#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切写权威校验器：核对每个服务「env 文件配置的模式」与「进程实际加载的模式」。

背景（老板 2026-09-16 批评）：HQ_<DOMAIN>_STORE 默认 sqlite/json，漏配即静默退回
旧存储，切写后可能双权威并存。本脚本是切写时的权威校验门：切完每个域、重启
对应服务后，用它确认**每个进程真正拿到的开关值**与 env 文件一致。

用法（在服务器上，需要 sudo 才能读其它 uid 的 /proc/<pid>/environ）：

    sudo /usr/bin/python3 scripts/check_store_authority.py
    sudo /usr/bin/python3 scripts/check_store_authority.py --expect sqlite
    sudo /usr/bin/python3 scripts/check_store_authority.py --expect postgres
    sudo /usr/bin/python3 scripts/check_store_authority.py --unit huangque-content

判读口径（每个 (服务, 开关) 组合）：

=====================  =========================================================
env 文件            进程实际                结论
=====================  =========================================================
未配置                默认值（缺失）          OK（注释「默认」）
未配置                非默认值               WARN（env 从别处注入，需人确认）
已配置 X              X                     OK
已配置 X              缺失（即默认）          ERROR：进程没吃到配置（多半没重启）
已配置 X              Y（≠X）               ERROR：进程与配置不一致
=====================  =========================================================

退出码：0 = 全部 OK；1 = 有 ERROR（或 --strict 下有 WARN）；
2 = 有 WARN；3 = 服务未运行 / env 文件不可读等环境错误。

MANIFEST 依据：各服务入口真实 import 的 store 模块（2026-09-16 核实）：

* huangque-content：flags/obs/channel/leads/admin_config/tikhub（content_api 全域）
* huangque-admin：flags/obs/channel/leads/admin_config（admin_api import 链）
* huangque-leadgen-api：flags/leads（leadgen_api 直 import）
* huangque-imggen-api：flags/channel（imggen_api 直 import）
* huangque-creator-agent：creator（独立服务唯一权威）
* hq-ip-agent：session（v4 会话权威）
* huangque-auth：identity/ledger（M6 切写后才生效，之前两开关应「未配置」）

本脚本只读：不写任何文件、不改任何配置、不重启任何服务。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# (systemd 单元, 该服务相关的 HQ_*_STORE 开关, 缺失时的进程内默认)
# 默认值必须与各 store 模块 mode() 里的 `or "默认"` 逐字一致。
MANIFEST = {
    "huangque-content": [
        ("HQ_FLAGS_STORE", "sqlite"),
        ("HQ_OBS_STORE", "sqlite"),
        ("HQ_CHANNEL_STORE", "sqlite"),
        ("HQ_LEADS_STORE", "sqlite"),
        ("HQ_ADMIN_CONFIG_STORE", "sqlite"),
        ("HQ_TIKHUB_CACHE", "sqlite"),
    ],
    "huangque-admin": [
        ("HQ_FLAGS_STORE", "sqlite"),
        ("HQ_OBS_STORE", "sqlite"),
        ("HQ_CHANNEL_STORE", "sqlite"),
        ("HQ_LEADS_STORE", "sqlite"),
        ("HQ_ADMIN_CONFIG_STORE", "sqlite"),
    ],
    "huangque-leadgen-api": [
        ("HQ_FLAGS_STORE", "sqlite"),
        ("HQ_LEADS_STORE", "sqlite"),
    ],
    "huangque-imggen-api": [
        ("HQ_FLAGS_STORE", "sqlite"),
        ("HQ_CHANNEL_STORE", "sqlite"),
    ],
    "huangque-creator-agent": [
        ("HQ_CREATOR_STORE", "sqlite"),
    ],
    "hq-ip-agent": [
        ("HQ_SESSION_STORE", "json"),
    ],
    "huangque-auth": [
        # M6 切写阶段才配置；在此之前期待「未配置」。
        ("HQ_IDENTITY_STORE", "sqlite"),
        ("HQ_LEDGER_STORE", "sqlite"),
        # M3A 切写后 auth 也是 flags 域的读方（feature_flags/flags_store 部署在
        # auth-service/content_domains），必须与 content 家族同值。
        ("HQ_FLAGS_STORE", "sqlite"),
    ],
}


def systemctl_show(unit: str, prop: str) -> str:
    out = subprocess.run(
        ["systemctl", "show", unit, "-p", prop, "--value"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError("systemctl show %s 失败: %s" % (unit, out.stderr.strip()))
    return out.stdout.strip()


def read_env_files(unit: str):
    """把单元的 EnvironmentFiles 链全部读出，返回 (env, 不可读清单, 缺失清单)。

    env: {var: (value, 来源)}。systemctl show 的列表属性名是复数
    ``EnvironmentFiles``，``--value`` 下**每项占一行**，形如
    ``/path (ignore_errors=no)``；``-`` 前缀或 ignore_errors=yes 表示文件可缺失。
    单元自身的 ``Environment=`` 行（含 drop-in）最后叠加上去（systemd 语义：
    Environment= 覆盖 EnvironmentFile），来源记为 ``<unit> Environment=``。
    """
    env = {}
    unreadable = []
    missing = []
    for raw in systemctl_show(unit, "EnvironmentFiles").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        ignore_errors = raw.startswith("-")
        if ignore_errors:
            raw = raw[1:]
        if raw.endswith(")"):
            head, _, tail = raw.rpartition(" (ignore_errors=")
            if tail.endswith(")") and head:
                ignore_errors = ignore_errors or tail[:-1] == "yes"
                raw = head
        if not raw:
            continue
        try:
            with open(raw, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key.startswith("export "):
                        key = key[len("export "):].strip()
                    env.setdefault(key, (value.strip().strip("'\""), raw))
        except PermissionError:
            unreadable.append(raw)
        except OSError:
            if not ignore_errors:
                missing.append(raw)
    # Environment= 项以空格分隔（值里的空格在 systemd 输出中是 \x20 转义）。
    for item in systemctl_show(unit, "Environment").split():
        item = item.replace("\\x20", " ")
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        env[key.strip()] = (value.strip().strip("'\""), "<%s> Environment=" % unit)
    return env, unreadable, missing


def read_proc_environ(pid: int):
    """读 /proc/<pid>/environ，返回 {var: value}。"""
    if pid <= 0:
        return None
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            data = fh.read()
    except (PermissionError, FileNotFoundError, OSError):
        return None
    env = {}
    for item in data.split(b"\0"):
        if not item:
            continue
        key, _, value = item.partition(b"=")
        env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def verdict(expected: str | None, actual: str | None, default: str):
    """按文档口径给出结论。返回 (结论词, 描述)。"""
    if expected is None and actual is None:
        return "OK", "默认 %s（env 未配置）" % default
    if expected is None:
        return "WARN", "env 未配置但进程拿到 %r（来源不明，需人确认）" % actual
    if actual is None:
        return "ERROR", "env 配了 %r，进程没吃到（多半没重启）" % expected
    if actual == expected:
        return "OK", "一致（%s）" % expected
    return "ERROR", "env=%r 进程=%r 不一致" % (expected, actual)


def check(units, expect=None, strict=False, as_json=False):
    results = {}
    exit_code = 0
    for unit in units:
        if unit not in MANIFEST:
            print("未知单元: %s（清单里没有）" % unit, file=sys.stderr)
            exit_code = max(exit_code, 3)
            continue
        try:
            active = systemctl_show(unit, "ActiveState")
            main_pid = int(systemctl_show(unit, "MainPID") or 0)
        except RuntimeError as exc:
            results[unit] = {"error": str(exc), "rows": []}
            exit_code = max(exit_code, 3)
            continue
        env_files, unreadable, missing = read_env_files(unit)
        proc_env = read_proc_environ(main_pid)
        rows = []
        for var, default in MANIFEST[unit]:
            expected = env_files.get(var, (None, None))[0]
            actual = proc_env.get(var) if proc_env is not None else None
            if proc_env is None and active == "active":
                conclusion, note = "ERROR", "/proc 不可读（需 sudo）"
            else:
                conclusion, note = verdict(expected, actual, default)
            if expect is not None:
                effective = actual if actual is not None else (expected if expected is not None else default)
                if effective != expect and conclusion == "OK":
                    conclusion, note = "ERROR", "期望 %r，实际 %r" % (expect, effective)
            if conclusion == "ERROR":
                exit_code = max(exit_code, 1)
            elif conclusion == "WARN":
                exit_code = max(exit_code, 2 if not strict else 1)
            rows.append({
                "var": var, "expected": expected, "actual": actual,
                "default": default, "conclusion": conclusion, "note": note,
            })
        if unreadable:
            for path in unreadable:
                rows.append({
                    "var": "(envfile)", "expected": None, "actual": None,
                    "default": "", "conclusion": "WARN",
                    "note": "env 文件不可读：%s" % path,
                })
                exit_code = max(exit_code, 2 if not strict else 1)
        if missing:
            for path in missing:
                rows.append({
                    "var": "(envfile)", "expected": None, "actual": None,
                    "default": "", "conclusion": "ERROR",
                    "note": "env 文件缺失（单元标注必须存在）：%s" % path,
                })
                exit_code = max(exit_code, 1)
        results[unit] = {
            "state": active, "main_pid": main_pid, "rows": rows,
        }

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for unit, info in results.items():
            if "error" in info:
                print("[%s] %s" % (unit, info["error"]))
                continue
            print("[%s] state=%s pid=%s" % (unit, info["state"], info["main_pid"]))
            for row in info["rows"]:
                exp = "-" if row["expected"] is None else row["expected"]
                act = "-" if row["actual"] is None else row["actual"]
                print("  %-22s env=%-10s proc=%-10s %-5s %s"
                      % (row["var"], exp, act, row["conclusion"], row["note"]))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", action="append", default=[],
                        help="只查指定 systemd 单元（可多次）")
    parser.add_argument("--expect", choices=["sqlite", "json", "postgres", "redis", "shadow"],
                        help="断言所有相关开关的实际生效值等于该值（切写门禁用）")
    parser.add_argument("--strict", action="store_true",
                        help="WARN 也按失败退出（默认 WARN 退出码 2）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()
    units = args.unit or list(MANIFEST)
    return check(units, args.expect, args.strict, args.json)


if __name__ == "__main__":
    sys.exit(main())
