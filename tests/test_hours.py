"""
When the screen should be lit.

The guarantee outranks the optimisation everywhere here: anything unclear
— no hours, a malformed day, an unreadable window — resolves to on. Only
an explicit closed day, or a time outside a clear window, powers it down.
"""

from __future__ import annotations

from datetime import UTC, datetime

from adnova_player.hours import screen_should_be_on

# A Saturday at 14:00 and 23:00, for a shop open 09:00–18:00 that day.
SAT_1400 = datetime(2026, 8, 15, 14, 0, tzinfo=UTC)  # weekday() == 5 (Saturday)
SAT_2300 = datetime(2026, 8, 15, 23, 0, tzinfo=UTC)
SAT_0830 = datetime(2026, 8, 15, 8, 30, tzinfo=UTC)

OPEN_SAT = {"saturday": {"open": True, "start": "09:00", "end": "18:00"}}


def test_no_hours_means_always_on():
    assert screen_should_be_on(None, SAT_2300) is True
    assert screen_should_be_on({}, SAT_2300) is True


def test_on_during_business_hours():
    assert screen_should_be_on(OPEN_SAT, SAT_1400) is True


def test_off_well_outside_business_hours():
    assert screen_should_be_on(OPEN_SAT, SAT_2300) is False


def test_the_margin_opens_the_screen_early():
    # 08:30 is before 09:00 but within the 15-minute margin... actually
    # 30 minutes before, so still off; 08:50 would be on.
    assert screen_should_be_on(OPEN_SAT, SAT_0830) is False
    assert screen_should_be_on(OPEN_SAT, datetime(2026, 8, 15, 8, 50, tzinfo=UTC)) is True


def test_an_explicitly_closed_day_is_off():
    hours = {"saturday": {"open": False}}
    assert screen_should_be_on(hours, SAT_1400) is False


def test_a_malformed_day_stays_on():
    # A garbled entry is not a deliberate "go dark", so the guarantee wins.
    assert screen_should_be_on({"saturday": "weird"}, SAT_1400) is True
    assert screen_should_be_on({"saturday": {"open": True}}, SAT_1400) is True  # no window


def test_a_day_not_in_the_map_stays_on():
    assert screen_should_be_on({"monday": {"open": True, "start": "9", "end": "17"}}, SAT_1400) is True
