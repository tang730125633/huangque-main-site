"""Versioned in-process Skill contracts for the IP12 coaching pipeline."""

import urllib.request

_UPSTREAM = (
    "https://raw.githubusercontent.com/tang730125633/huangque-main-site/"
    "0075b6cd504dbb8b101afaaf7947293ca03919b5/server/hermes_ip12/coaching_skills.py"
)
_SRC = urllib.request.urlopen(_UPSTREAM, timeout=30).read().decode("utf-8")
exec(compile(_SRC, __file__, "exec"), globals())
import ip12_conversational_intake  # noqa: F401
