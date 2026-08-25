"""
Shared test fixtures.

The event log is process-wide by design — the admin page, the HTTP client and
the enrollment flow all record into one place without being handed a reference.
That is right in production and hostile in a test suite, where a log adopted by
one test would still be pointing at another test's temp directory. Resetting it
around every test keeps the isolation the rest of the suite assumes.
"""

from __future__ import annotations

import pytest

from adnova_player import event_log


@pytest.fixture(autouse=True)
def _isolated_event_log():
    event_log.reset_for_tests()
    yield
    event_log.reset_for_tests()
