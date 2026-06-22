"""Turn a datetime into a short 'last heard' phrase."""
from __future__ import annotations

from datetime import datetime, timezone


def humanize(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "no record"
    if now is None:
        now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    # Make both comparable.
    if when.tzinfo and now.tzinfo is None:
        now = now.replace(tzinfo=when.tzinfo)
    if now.tzinfo and when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)

    delta = now - when
    secs = delta.total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "moments ago"
    mins = secs / 60
    if mins < 60:
        n = int(round(mins))
        return f"{n} min ago" if n != 1 else "1 min ago"
    hours = mins / 60
    if hours < 24:
        n = int(hours)
        return f"{n} hr ago" if n != 1 else "1 hr ago"
    days = hours / 24
    if days < 7:
        n = int(days)
        return f"{n} days ago" if n != 1 else "yesterday"
    # Older than a week: give the date.
    return when.strftime("%b %-d") if hasattr(when, "strftime") else "a while ago"
