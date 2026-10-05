"""What day it is, for the business - not for the server.

THE BUG THIS EXISTS TO PREVENT
------------------------------
`date.today()` answers in the server's timezone, and a container's timezone is
UTC. India is UTC+5:30, so for five and a half hours every night the server
believes it is still yesterday. A site supervisor marking the crew at 00:30
IST sends today's date, the server compares it with ITS today, and refuses the
day as "marked before it happens". The same window dated approved advances on
the wrong day, rejected a settlement that ended today, and drew the analytics
chart one day short.

Every "today" in the app goes through `business_today()`, which reads the
calendar in `Settings.business_timezone` (Asia/Kolkata unless configured).

TESTS
-----
`_now` is the single clock source, so a test can pin the moment with
`monkeypatch.setattr(clock, "_now", ...)` and exercise the midnight window
directly rather than waiting for it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import get_settings


@lru_cache
def business_zone() -> ZoneInfo:
    # tzdata (a pip dependency) supplies the zone database on Windows and in
    # slim containers, neither of which ships /usr/share/zoneinfo.
    return ZoneInfo(get_settings().business_timezone)


def _now() -> datetime:
    """The current instant, timezone-aware. Patched by tests."""
    return datetime.now(timezone.utc)


def business_now() -> datetime:
    """The current wall-clock time where the business is."""
    return _now().astimezone(business_zone())


def business_today() -> date:
    """Today's date where the business is. Use this, never `date.today()`."""
    return business_now().date()
