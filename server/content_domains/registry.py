"""Capability registry assembled from domain modules."""

from . import (
    audio, breakdown, canvas_agent, image, leads, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect, text, video,
)


HANDLERS = {}
for domain in (
    image, text, canvas_agent, leads, audio, video, breakdown, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect,
):
    HANDLERS.update(domain.HANDLERS)
