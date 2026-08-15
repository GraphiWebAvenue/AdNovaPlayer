"""
Deciding whether the shop is open right now.

The screen powers down outside operating hours, and those hours come from
the stand itself — the same `operating_hours` Dashboard already keeps,
carried in the manifest so the device evaluates them locally and needs no
network to know when to sleep.

The shape matches Dashboard's exactly: a map of lowercase weekday names to
`{open: bool, start: "HH:MM", end: "HH:MM"}`. A day that is closed, or a
manifest with no hours at all, means "always on" — because a lit screen
during business hours is the thing that must never fail, and powering
down is only ever the optimisation.
"""

from __future__ import annotations

from datetime import datetime

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def screen_should_be_on(
    operating_hours: dict | None,
    moment: datetime,
    margin_minutes: int = 15,
) -> bool:
    """
    Whether the display should be lit at `moment`, in the stand's own time.

    `moment` must already be in the stand's local timezone — the caller
    converts, because only it knows which zone the manifest named. The
    margin opens the screen a little before opening and holds it a little
    after closing, so it is never caught dark by an early customer or a
    clock a minute slow.

    Missing or unparseable hours mean on. The optimisation defers to the
    guarantee.
    """
    if not operating_hours:
        return True

    today = operating_hours.get(_DAYS[moment.weekday()])

    # A malformed or absent entry is not a deliberate instruction to go
    # dark, so it stays on. Only an explicit open:false does that.
    if not isinstance(today, dict):
        return True
    if today.get("open") is False:
        return False
    if not today.get("open"):
        return True  # neither clearly open nor clearly closed → stay on

    start = _minutes(today.get("start"))
    end = _minutes(today.get("end"))
    if start is None or end is None:
        return True  # can't read the window; stay on

    now = moment.hour * 60 + moment.minute
    return (start - margin_minutes) <= now <= (end + margin_minutes)


def _minutes(value: object) -> int | None:
    """Parse "HH:MM" to minutes past midnight, or None if it is not that."""
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        hours, minutes = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes
