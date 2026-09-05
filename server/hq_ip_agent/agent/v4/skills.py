"""v4 技能加载：12 个业务 skill（subagents/<id>/SKILL.md 源文件）+
Penguin 子 Agent 的 AGENTS.md + use-huangque-cli 官方工具底座。

- 子 Agent system prompt 由 AGENTS.md + 业务 SKILL 全文 + use-huangque-cli 全文组装；
- 主 Agent 只拿到每个业务 skill 的 frontmatter 摘要（渐进披露），
  识别意图后通过 read_skill 工具展开全文。
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SUBAGENTS_DIR = os.path.join(_ROOT, "subagents")

# Penguin agents 目录（AGENTS.md / use-huangque-cli 安装副本所在），可用环境变量覆盖
PENGUIN_AGENTS_DIR = os.environ.get(
    "HQ_AGENTS_DIR",
    os.path.expanduser("~/.penguin/data/default_project/agents"),
)

# 12 个业务域：目录名（skill 源）→ 子 Agent id
DOMAINS = {
    "image": "hq-image",
    "video": "hq-video",
    "audio": "hq-audio",
    "copy": "hq-copy",
    "digital-human": "hq-digital-human",
    "short-drama": "hq-short-drama",
    "compose": "hq-compose",
    "canvas": "hq-canvas",
    "leads": "hq-leads",
    "collect": "hq-collect",
    "ip-positioning": "hq-ip-positioning",
    "system": "hq-system",
}

_BUSINESS_SKILL_NAME = {  # 业务 skill 的安装名（<domain>-business）
    d: f"{d}-business" for d in DOMAINS
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_KV_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$')


def _parse_frontmatter(text: str):
    meta = {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return meta, text
    for line in m.group(1).splitlines():
        kv = _KV_RE.match(line.strip())
        if kv:
            k, v = kv.groups()
            meta[k] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 业务 skill（源文件在项目 subagents/ 下，可直接编辑；安装副本由源复制）
# ---------------------------------------------------------------------------

def business_skill_path(domain: str) -> str:
    return os.path.join(_SUBAGENTS_DIR, DOMAINS[domain], "SKILL.md")


def load_business_skill(domain: str) -> tuple[dict, str]:
    """返回 (frontmatter, 正文)。找不到时返回空。"""
    text = _read(business_skill_path(domain)) or ""
    return _parse_frontmatter(text)


def business_skill_summary(domain: str) -> dict:
    """主 Agent 渐进披露用的摘要：只取 frontmatter 的名称与描述。"""
    meta, body = load_business_skill(domain)
    return {
        "domain": domain,
        "agent_id": DOMAINS[domain],
        "skill_name": meta.get("name", ""),
        "short_description_zh": meta.get("short_description_zh", ""),
        "short_description": meta.get("short_description", ""),
        "description": (meta.get("description", "") or "")[:200],
        "version": meta.get("version", ""),
    }


def load_agents_md(domain: str) -> str:
    """子 Agent 角色/领域规则（Penguin agent_state/AGENTS.md）。"""
    path = os.path.join(PENGUIN_AGENTS_DIR, DOMAINS[domain], "agent_state", "AGENTS.md")
    return _read(path) or ""


# ---------------------------------------------------------------------------
# 业务 skill 安装副本校验（#20）：项目 subagents/ 源 → 本机 Penguin 子 Agent
# 安装副本。scripts/sync_skills.py 负责推送，本函数供启动时校验告警。
# ---------------------------------------------------------------------------

def installed_business_skill_path(domain: str) -> str:
    """本机 Penguin 子 Agent 的 <domain>-business 安装副本路径。"""
    return os.path.join(
        PENGUIN_AGENTS_DIR, DOMAINS[domain], "agent_state", "skills",
        _BUSINESS_SKILL_NAME[domain], "SKILL.md",
    )


def _sha256(path: str) -> str | None:
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def business_skill_sync_status() -> list[tuple[str, str]]:
    """逐域对比源与安装副本，返回 [(domain, "ok"|"missing"|"diff"|"no-source")]。

    Penguin agents 目录不存在（服务器无副本，子 Agent 为应用内 LLM 实例）时
    返回空列表，调用方静默跳过。
    """
    if not os.path.isdir(PENGUIN_AGENTS_DIR):
        return []
    out = []
    for domain in DOMAINS:
        src_hash = _sha256(business_skill_path(domain))
        if src_hash is None:
            out.append((domain, "no-source"))
            continue
        dst_hash = _sha256(installed_business_skill_path(domain))
        if dst_hash is None:
            out.append((domain, "missing"))
        elif dst_hash == src_hash:
            out.append((domain, "ok"))
        else:
            out.append((domain, "diff"))
    return out


def load_use_huangque_cli(domain: str) -> str:
    """官方工具底座技能全文（Penguin 安装副本）。"""
    path = os.path.join(
        PENGUIN_AGENTS_DIR, DOMAINS[domain], "agent_state", "skills",
        "use-huangque-cli", "SKILL.md",
    )
    text = _read(path)
    if text:
        _, body = _parse_frontmatter(text)
        return body.strip()
    return ""
