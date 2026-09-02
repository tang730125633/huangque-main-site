"""Capability registry assembled from domain modules."""

import importlib

from . import (
    audio, breakdown, canvas_agent, image, leads, matrix_template_video, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect, text, video,
)


HANDLERS = {}
for domain in (
    image, text, canvas_agent, leads, audio, video, breakdown, matrix_template_video, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect,
):
    HANDLERS.update(domain.HANDLERS)

# The customer guide remains optional so a missing CLI or model dependency
# cannot prevent the rest of content-api from starting.
try:
    HANDLERS.update(importlib.import_module(
        ".director_agent", __package__,
    ).HANDLERS)
except Exception as error:
    print("[registry] optional director_agent unavailable: %s" % error, flush=True)
