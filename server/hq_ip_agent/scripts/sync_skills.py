#!/usr/bin/env python3
"""业务 skill 同步（#20）：项目 subagents/<id>/SKILL.md（源）→ 本机 Penguin 子 Agent
安装副本 agent_state/skills/<domain>-business/SKILL.md。

默认 --check：逐域哈希对比，打印差异；有不同步时退出码 1（供 CI/冒烟判断）。
--push：把源复制到安装副本（目录自动创建），并打印做了什么。
服务器没有 Penguin 副本目录，两个模式都静默通过（子 Agent 是应用内 LLM 实例）。

运行：.venv/bin/python scripts/sync_skills.py [--push]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agent.v4 import skills  # noqa: E402


def _push(domain: str, status: str) -> None:
    src = skills.business_skill_path(domain)
    dst = skills.installed_business_skill_path(domain)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    print("  push %-14s %s → %s" % (status, src, dst))


def main() -> int:
    ap = argparse.ArgumentParser(description="同步业务 skill 到本机 Penguin 子 Agent 副本")
    ap.add_argument("--push", action="store_true", help="把源复制到安装副本（默认只检查）")
    args = ap.parse_args()

    statuses = skills.business_skill_sync_status()
    if not statuses:
        print("本机没有 Penguin agents 目录（%s），跳过（服务器场景无需同步）。"
              % skills.PENGUIN_AGENTS_DIR)
        return 0

    ok = bad = 0
    for domain, status in sorted(statuses):
        if status == "ok":
            ok += 1
        else:
            bad += 1
            if args.push:
                _push(domain, status)
            else:
                print("  %-10s %s（源=%s 副本=%s）" % (
                    status, domain, skills.business_skill_path(domain),
                    skills.installed_business_skill_path(domain)))

    if args.push:
        print("推送完成：%d 个一致，%d 个已更新。" % (ok, bad))
        return 0
    if bad:
        print("共 %d 个不同步，%d 个一致。运行 scripts/sync_skills.py --push 同步。"
              % (bad, ok))
        return 1
    print("全部一致：%d 个业务 skill 副本与源同步。" % ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
