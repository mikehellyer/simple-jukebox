"""Small formatting helpers shared by the GUI."""
from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Render a length as m:ss, or h:mm:ss once it reaches an hour."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
