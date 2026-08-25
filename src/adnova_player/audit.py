"""
The safety net under the forensic trail.

The rest of the player records events deliberately: a refused manifest, a
failed login, a command run. Deliberate is better — the codes are a closed
vocabulary Dashboard can alert on, and the details are written to be read.

But deliberate is also a list somebody has to keep complete, and the whole
point of a forensic trail is the incident nobody anticipated. So every
warning-or-worse this process logs is *also* mirrored into the event log,
under a code derived from the module that raised it. A future contributor
who adds a `log.error(...)` and never thinks about auditing still leaves a
trace at Dashboard.

Two rules keep the net from becoming the flood:

**The events logger is excluded.** It reports its own failures through
`logging`; forwarding those back into itself is a loop.

**Codes are per-module, not per-message.** `log.api`, `log.cache`,
`log.agent` — coarse on purpose, so the event log's per-code rate limit
actually bites on a module that has gone into a retry storm.
"""

from __future__ import annotations

import logging

from . import event_log

# Levels below this never become events; the journal keeps them.
MIRROR_LEVEL = logging.WARNING

# Its own failures arrive through logging; mirroring them would recurse.
EXCLUDED = ("adnova.events",)


class AuditHandler(logging.Handler):
    """Mirror WARNING-and-above into the process-wide event log."""

    def __init__(self) -> None:
        super().__init__(level=MIRROR_LEVEL)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name in EXCLUDED or record.name.startswith(EXCLUDED[0] + "."):
                return

            # "adnova.agent" -> "agent"; a bare "adnova" or a third-party
            # logger keeps its full name, which is the useful thing to see.
            name = record.name
            module = name.split("adnova.", 1)[1] if name.startswith("adnova.") else name

            event_log.record(
                f"log.{module}",
                "error" if record.levelno >= logging.ERROR else "warn",
                self.format(record),
            )
        except Exception:  # pragma: no cover - a handler must never raise
            self.handleError(record)


def install(root: str = "") -> AuditHandler:
    """
    Attach the mirror. Idempotent — a second call replaces the first.

    Attached to the root logger by default rather than to "adnova", because a
    library that fails loudly (httpx refusing a certificate, uvicorn giving up
    on a socket) is exactly the kind of thing worth having at Dashboard when a
    device misbehaves.
    """
    logger = logging.getLogger(root)
    for existing in list(logger.handlers):
        if isinstance(existing, AuditHandler):
            logger.removeHandler(existing)

    handler = AuditHandler()
    # Just the message: the event carries its own timestamp, severity and
    # source, so repeating the journal's prefix would only eat the 200
    # characters the contract allows for the detail.
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return handler
