"""
What the device knows about itself.

Everything here feeds the heartbeat, which is the only window an operator
has into a Pi they cannot reach: it is behind the shop's NAT and, by
design, accepts no inbound connection. So the richer this picture, the
sooner someone spots a screen about to fail — a Pi that is throttling in
a hot window, a card filling up, a clock drifting past the signing window.

Every reading is best-effort. A sensor that is missing on this hardware,
or a command that is not present, yields None rather than raising —
because a heartbeat that fails to assemble is worse than one with a blank
temperature, and the whole point is to keep hearing from a device that is
in trouble.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("adnova.health")

# Pi's firmware throttle bits, from `vcgencmd get_throttled`. The low bits
# are "happening now", the high bits "has happened since boot".
_THROTTLE_UNDERVOLT_NOW = 0x1
_THROTTLE_CAP_NOW = 0x2
_THROTTLE_THROTTLED_NOW = 0x4
_THROTTLE_TEMP_LIMIT_NOW = 0x8


@dataclass(frozen=True)
class Health:
    """A snapshot, all fields optional because any sensor may be absent."""

    temp_c: float | None = None
    cpu_percent: float | None = None
    mem_percent: float | None = None
    disk_free_bytes: int | None = None
    cache_used_bytes: int | None = None
    uptime_seconds: int | None = None
    undervoltage: bool | None = None
    throttled: bool | None = None
    warnings: list[str] = field(default_factory=list)


def read(cache_dir: Path, cache_used_bytes: int | None = None) -> Health:
    """
    Assemble a health snapshot. Never raises.

    `cache_used_bytes` is passed in rather than recomputed, because the
    cache already knows it and walking the directory twice a minute is
    waste on a small card.
    """
    warnings: list[str] = []

    temp = _temperature()
    cpu, mem = _cpu_mem()
    disk_free = _disk_free(cache_dir)
    uptime = _uptime()
    under, throttled = _throttle_state()

    # Turn the raw readings into the plain-language flags an operator scans.
    if under:
        warnings.append("undervoltage — check the power supply")
    if throttled:
        warnings.append("CPU is being throttled — likely overheating")
    if temp is not None and temp >= 80:
        warnings.append(f"high temperature: {temp:.0f}°C")
    if disk_free is not None and disk_free < 200 * 1024 * 1024:
        warnings.append("low disk space")

    return Health(
        temp_c=temp,
        cpu_percent=cpu,
        mem_percent=mem,
        disk_free_bytes=disk_free,
        cache_used_bytes=cache_used_bytes,
        uptime_seconds=uptime,
        undervoltage=under,
        throttled=throttled,
        warnings=warnings,
    )


# ── Sensors ──────────────────────────────────────────────────────────────


def _temperature() -> float | None:
    # The thermal zone is the portable way; vcgencmd is the Pi-specific
    # one. Try the file first — it needs no subprocess.
    zone = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        if zone.exists():
            milli = int(zone.read_text().strip())
            return round(milli / 1000, 1)
    except (OSError, ValueError):
        pass

    out = _vcgencmd("measure_temp")  # e.g. "temp=48.3'C"
    if out and "=" in out:
        try:
            return float(out.split("=", 1)[1].split("'", 1)[0])
        except ValueError:
            return None
    return None


def _cpu_mem() -> tuple[float | None, float | None]:
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        # A short interval so the reading reflects now without stalling the
        # heartbeat for a full second.
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory().percent
        return round(cpu, 1), round(mem, 1)
    except Exception:  # noqa: BLE001 — a health read must never take the loop down
        return None, None


def _disk_free(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _uptime() -> int | None:
    try:
        return int(time.monotonic())
    except OSError:
        return None


def _throttle_state() -> tuple[bool | None, bool | None]:
    out = _vcgencmd("get_throttled")  # e.g. "throttled=0x50000"
    if not out or "=" not in out:
        return None, None
    try:
        bits = int(out.split("=", 1)[1].strip(), 16)
    except ValueError:
        return None, None

    under = bool(bits & _THROTTLE_UNDERVOLT_NOW)
    throttled = bool(bits & (_THROTTLE_THROTTLED_NOW | _THROTTLE_TEMP_LIMIT_NOW | _THROTTLE_CAP_NOW))
    return under, throttled


def _vcgencmd(arg: str) -> str | None:
    """Run one vcgencmd, or None if it is not this hardware."""
    binary = shutil.which("vcgencmd")
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — fixed binary, fixed args
            [binary, arg],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
