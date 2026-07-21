#!/usr/bin/env python3
"""Serve local site files and proxy API calls to one fixed test backend."""

import urllib.parse


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def normalize_upstream(value):
    parsed = urllib.parse.urlsplit((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("upstream must be an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("upstream credentials are not allowed")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("upstream must not contain a path, query, or fragment")
    return parsed._replace(path="")


def rewrite_set_cookie(value, local_http=True):
    pieces = [piece.strip() for piece in value.split(";")]
    kept = []
    for index, piece in enumerate(pieces):
        lowered = piece.lower()
        if index > 0 and lowered.startswith("domain="):
            continue
        if index > 0 and local_http and lowered == "secure":
            continue
        kept.append(piece)
    return "; ".join(kept)


def safe_request_path(value):
    return urllib.parse.urlsplit(value).path


def is_hop_by_hop(name):
    return name.lower() in HOP_BY_HOP
