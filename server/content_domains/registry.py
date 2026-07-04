"""Capability registry assembled from domain modules."""

from . import audio, image, leads, text, video


HANDLERS = {}
for domain in (image, text, leads, audio, video):
    HANDLERS.update(domain.HANDLERS)
