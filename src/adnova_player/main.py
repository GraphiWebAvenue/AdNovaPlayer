"""
The process entry point.

Wires the pieces together and hands the HTTP server to uvicorn: config is
read (and a bad one stops us here, loudly, rather than limping on), the
brain's loops start, and the local server runs in the foreground as the
thing systemd watches.

Kept deliberately thin. Everything testable lives in the modules this
imports; this file is the plumbing that only makes sense with a real
socket and a real clock, so it carries no logic worth a unit test — just
the wiring and the top-level guard that keeps a crash from being the last
word.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from .agent import Agent
from .api import DashboardApi
from .cache import MediaCache
from .config import Config, ConfigError, load
from .playback_log import PlaybackLog
from .server import build_app

log = logging.getLogger("adnova")


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("ADNOVA_LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def build(config: Config) -> tuple[Agent, object]:
    """Assemble the agent and the ASGI app. Separated so a smoke test can too."""
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    api = DashboardApi(config)
    cache = MediaCache(config.media_dir)
    playback = PlaybackLog(config.log_dir / "playback.json")

    agent = Agent(config, api, cache, playback)
    app = build_app(
        cache,
        current_schedule=agent.schedule,
        on_playing=agent.on_playing,
    )
    return agent, app


def main() -> int:
    _configure_logging()

    try:
        config = load()
    except ConfigError as exc:
        # A misconfigured device has nothing safe to do. Exit non-zero so
        # systemd shows it failed rather than looping a broken start.
        log.error("Cannot start: %s", exc)
        return 2

    if not config.trusted_keys:
        log.warning(
            "No manifest signing keys are provisioned. Once Dashboard "
            "requires signatures this device will refuse every manifest — "
            "provision ADNOVA_TRUSTED_KEYS before then."
        )

    agent, app = build(config)
    agent.start()

    # A clean stop on SIGTERM (systemd stop / restart) so the last plan and
    # playback log are flushed rather than torn.
    def _shutdown(_signum, _frame):
        log.info("Shutting down.")
        agent.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    import uvicorn

    uvicorn.run(
        app,
        host=config.local_host,
        port=config.local_port,
        log_level="warning",  # our own logging carries the detail
        access_log=False,
        server_header=False,
    )
    return 0


def run_forever() -> None:
    """
    The last line of defence.

    uvicorn.run returns only on shutdown. If main() ever falls through for
    a reason systemd should retry, this ensures the process exits non-zero
    so the unit's Restart=always brings it back rather than leaving a dark
    screen. Belt to systemd's braces.
    """
    try:
        code = main()
    except Exception:  # noqa: BLE001 — top-level guard; nothing above catches
        log.exception("Fatal error in the player; exiting for a restart.")
        time.sleep(2)  # avoid a tight crash-restart spin
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    run_forever()
