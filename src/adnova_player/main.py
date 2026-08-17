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
from pathlib import Path

from .agent import Agent
from .api import DashboardApi
from .cache import MediaCache
from .config import Config, ConfigError, load
from .config import enrollment as read_enrollment
from .playback_log import PlaybackLog
from .server import build_app

ENV_FILE = Path(os.environ.get("ADNOVA_ENV_FILE", "/etc/adnova-player/env"))

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

    agent = Agent(config, api, cache, playback, on_auth_lost=_reenroll)
    app = build_app(
        cache,
        current_schedule=agent.schedule,
        on_playing=agent.on_playing,
        # Resolve "what plays now" against Dashboard's true time, corrected for
        # a wrong device clock, so a Pi with no RTC still plays the right slot.
        now=agent._trusted_now,
    )

    # The on-site status page, behind the admin login.
    from .admin import attach_admin

    attach_admin(app, config, status=agent.status, on_action=agent.trigger)

    return agent, app


def main() -> int:
    _configure_logging()

    try:
        config = load()
    except ConfigError as exc:
        # No stand key. If the device was imaged for enrollment, this is
        # not an error — it is a blank device waiting to be adopted. Run
        # the enrollment flow; it returns only once the device has a key,
        # after which we restart into normal operation.
        enroll = read_enrollment()
        if enroll is not None:
            return _enroll_then_restart(enroll)

        # Otherwise a misconfigured device has nothing safe to do. Exit
        # non-zero so systemd shows it failed rather than looping.
        log.error("Cannot start: %s", exc)
        return 2

    if not config.trusted_keys:
        log.warning(
            "No manifest signing keys are provisioned. Once Dashboard "
            "requires signatures this device will refuse every manifest — "
            "provision ADNOVA_TRUSTED_KEYS before then."
        )

    agent, app = build(config)
    # Look ourselves over before opening for business: the result is logged
    # here and rides every heartbeat, but a failed check never stops us — a
    # signage box shows something before it complains.
    agent.run_self_test()
    agent.start()

    # Tell systemd we are up, then feed its watchdog only while the loops
    # are genuinely making progress — see watchdog.py for why an
    # unconditional ping would be worse than none.
    from .watchdog import Watchdog, ready

    ready()
    watchdog = Watchdog(is_live=agent.is_live)
    watchdog.start()

    # A clean stop on SIGTERM (systemd stop / restart) so the last plan and
    # playback log are flushed rather than torn.
    def _shutdown(_signum, _frame):
        log.info("Shutting down.")
        watchdog.stop()
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


def _enroll_then_restart(enroll) -> int:
    """
    Introduce a blank device and wait until it is adopted.

    While it waits, a splash server runs so the TV shows the AdNova screen
    rather than a desktop or an error — the device looks like it is setting
    itself up, because it is. Once adopted, the stand key is written and the
    service is restarted into normal operation; systemd's EnvironmentFile
    picks up the new credentials on that restart.
    """
    import threading
    import time

    from .enroll import Adoption, Enroller, write_credentials

    log.info("No stand key yet — entering enrollment. Waiting to be adopted in Dashboard.")

    # A splash so the screen is not blank while an admin adopts the device.
    splash_cache = MediaCache(enroll.cache_dir / "media")
    splash = _splash_server(splash_cache, enroll)
    threading.Thread(target=splash, daemon=True).start()

    enroller = Enroller(enroll.base_url, enroll.token, enroll.cache_dir / "enroll")
    client = enroller.client()

    introduced = False
    while True:
        if not introduced:
            status = enroller.introduce(client)
            introduced = status in ("pending", "approved")
            if status == "closed":
                introduced = False  # keep trying until an admin opens the door

        result = enroller.poll(client)
        if isinstance(result, Adoption):
            log.info("Adopted onto stand %s. Writing credentials.", result.stand_id)
            write_credentials(ENV_FILE, result)
            enroller.confirm_claimed(client)
            client.close()
            # Restart into normal operation; the new env is read on start.
            _restart_service()
            return 0

        if result == "rejected":
            log.warning("This device was rejected. Waiting in case that changes.")

        time.sleep(10)


def _splash_server(cache: MediaCache, enroll):
    """A tiny server that shows only the splash, for the enrollment wait."""
    from .schedule import Schedule

    app = build_app(cache, current_schedule=lambda: Schedule(None, cache))

    def run() -> None:
        import uvicorn

        uvicorn.run(app, host=enroll.local_host, port=enroll.local_port,
                    log_level="warning", access_log=False, server_header=False)

    return run


def _reenroll() -> None:
    """
    Drop a rejected stand key and restart into enrollment for a fresh one.

    Called when Dashboard has refused the key for long enough that it is
    plainly gone (stand reassigned or deleted, key rotated). The device's own
    identity — its device_id and secret — is left untouched, so Dashboard can
    re-adopt the very same device; only the dead stand key is cleared, and the
    restart lands in the enrollment flow because a keyless config cannot load.
    """
    _clear_stand_key(ENV_FILE)
    _restart_service()


def _clear_stand_key(env_path: Path) -> None:
    """Remove the ADNOVA_STAND_KEY line, leaving every other setting intact."""
    if not env_path.exists():
        return
    kept = [
        line for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("ADNOVA_STAND_KEY=")
    ]
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(env_path)


def _restart_service() -> None:
    import subprocess

    try:
        subprocess.run(  # noqa: S603,S607 — fixed argv
            ["sudo", "-n", "systemctl", "restart", "adnova-player"], timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Could not restart after enrollment: %s", exc)


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
