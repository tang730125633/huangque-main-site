"""Capability registry assembled from domain modules."""

from . import audio, breakdown, image, leads, text, video


HANDLERS = {}
for domain in (image, text, leads, audio, video, breakdown):
    HANDLERS.update(domain.HANDLERS)
