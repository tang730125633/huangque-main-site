#!/usr/bin/env bash
# 后端专项单测（B 批：persist 并发/livecaps 锁外/status 缓存/注册表回收），39 项断言。
set -u
cd "$(dirname "$0")"
cd ..
.venv/bin/python tests/hq-p0c-test.py
