"""Structured visual-only prompt compiler for short-drama video generation."""

import hashlib


PROMPT_TEMPLATE_VERSION = "short_drama_visual_only_v1"
VISUAL_ONLY_CONSTRAINT = (
    "\n\n[VISUAL-ONLY PRODUCTION CONSTRAINTS]\n"
    "- Generate picture only; do not generate dialogue, narration, singing, "
    "music, ambient sound, sound effects, captions, or invented text.\n"
    "- Do not invent, rewrite, quote, or perform any spoken lines.\n"
    "- Characters remain naturally silent with a relaxed closed mouth or "
    "subtle neutral expression; final lip movement is added from locked audio.\n"
    "- Preserve the specified characters, scene, action, framing, continuity, "
    "duration, and aspect ratio."
)


def compile_visual_only_prompt(prompt):
    source = str(prompt or "").strip()
    if not source:
        raise ValueError("video prompt is required")
    compiled = source + VISUAL_ONLY_CONSTRAINT
    return {
        "prompt": compiled,
        "template_version": PROMPT_TEMPLATE_VERSION,
        "compiled_prompt_hash": hashlib.sha256(
            compiled.encode("utf-8")
        ).hexdigest(),
    }
