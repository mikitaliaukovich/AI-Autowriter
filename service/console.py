"""Console encoding.

Windows consoles default to a legacy code page (cp1252 here), which raises
UnicodeEncodeError on the first Cyrillic character. Since essentially every log line,
transcript and error message in this project is Russian, every entry point switches
its streams to UTF-8 before doing anything else.
"""
from __future__ import annotations

import sys


def enable_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
