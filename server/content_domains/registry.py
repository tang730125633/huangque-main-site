"""Capability registry assembled from domain modules."""

from . import audio, breakdown, canvas_agent, image, leads, script_to_video, text, video


HANDLERS = {}
for domain in (image, text, canvas_agent, leads, audio, video, breakdown, script_to_video):
    HANDLERS.update(domain.HANDLERS)
