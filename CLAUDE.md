# Player — Working Rules

Advertisement playback application. Runs unattended on Raspberry
Pi devices installed at each stand. Fetches its schedule from
Dashboard, downloads media, plays ads, and reports status back.

Root CLAUDE.md rules always apply — this file only refines them.

---

## Purpose

The Player turns Dashboard's schedule into pixels on the LED wall
or monitor at each stand. It must:

- Play the right ad at the right time.
- Survive network outages without ever showing a black screen.
- Report enough status back that admins can spot problems remotely.
- Update itself safely from a Dashboard-signed release channel.

The device is physically at a customer site and typically not
easily accessible — every design decision optimises for "works
unattended for months".

---

## Stack

- **Python 3.12** on Raspberry Pi OS (bookworm or newer)
- **systemd** for lifecycle (main service + watchdog timer)
- **VLC** (via python-vlc) as the primary media backend; **mpv**
  as a fallback tested path
- **httpx** for API calls to Dashboard
- **sqlite3** (stdlib) for the local schedule / media cache index
- **structlog** for logging (JSON to `/var/log/adnova-player/`)
- **pytest** for tests (development on x86 with hardware mocks;
  CI includes a real-Pi smoke test)

Do NOT introduce a heavy runtime (Node, Java, .NET) — Pi resources
are tight and simple wins.

---

## Directory conventions

```
player/
├── src/
│   ├── scheduler/          — reads manifest, decides what's next
│   ├── media/              — download, verify checksums, cache eviction
│   ├── playback/           — driver for VLC / mpv
│   ├── reporter/           — heartbeat + playback log HTTP
│   ├── watchdog/           — restarts VLC / self on hang
│   ├── config/             — env + local config
│   └── main.py             — entry point
├── scripts/                — install, provision, factory-reset
├── systemd/                — .service and .timer units
├── fallback/               — bundled loop shown before first manifest
├── tests/
│   ├── unit/
│   └── smoke/              — runs on a real Pi in CI
└── contracts/              — Pydantic mirrors of Dashboard's Player API
```

---

## Boundary rules

- **Never write to Dashboard's DB directly.** Every outbound call
  goes through a documented HTTP endpoint under
  `Dashboard/api/player/*`.
- **Never trust the network.** Assume any HTTP call can fail.
  Cache the last successful manifest and keep playing it.
- **Never assume clock accuracy** without NTP. Sync at boot; if
  NTP fails, warn and continue with a monotonic-time fallback
  (advance the schedule from the last known good time).
- **Never fetch or execute code Dashboard didn't sign.** Media
  files are content, not code — VLC parses them, but they never
  reach the shell. Code updates come through the OS update
  mechanism (`apt` from a signed repo), not the ad channel.
- **Never persist customer PII** (viewer counts, camera data if
  we ever add one) locally. Aggregate then send.

---

## Playback contract

- Fetch manifest every N minutes (default 10, configurable).
  Preload 6 hours of media ahead of playback.
- Between manifest fetches, follow the local schedule cache.
  Never call Dashboard on the hot path of the "what plays next"
  decision — that decision must resolve in < 50 ms from local
  data.
- If a media file is missing when its slot arrives, skip to the
  next slot and log a `media_missing` event. Never freeze on
  black frames or show error text on the display.
- On boot, if there's no manifest yet, play the fallback loop
  (bundled with the installer) until the first manifest arrives.
- Ads have priority: `urgent` > `manual` > `template` > `ai`.
  If two slots overlap for any reason (contract bug, clock skew,
  hand-edit race), the higher-priority one wins.

---

## Heartbeat contract

Every 60 seconds (configurable):

```
POST /api/player/heartbeat
{
    "stand_id": "...",
    "player_version": "1.2.3",
    "current_slot_id": 123 | null,
    "current_ad_id": 456 | null,
    "disk_free_bytes": 12345678,
    "temp_c": 42.5,
    "uptime_seconds": 86400,
    "network_ok": true,
    "clock_offset_seconds": 0
}
```

Every played ad batch (or every 5 minutes, whichever comes
sooner):

```
POST /api/player/playback
{
    "entries": [
        {
            "slot_id": ...,
            "started_at": "...",
            "ended_at": "...",
            "completed": true | false,
            "reason": "played" | "skipped" | "media_missing"
                    | "stand_closed" | "interrupted"
        },
        ...
    ]
}
```

Both are best-effort. Failure to POST is retried on the next
tick; data is queued locally with a bounded ring so old logs are
dropped before disk fills.

---

## Communication with the user

Same as root: Persian prose in the assistant reply, English in
code, log messages, systemd units, and commit messages.

---

## Development behavior

- Test on a real Pi before merging anything that touches
  playback, hardware access, or systemd. Emulation on x86 catches
  most bugs but not display quirks, audio backends, or HDMI-CEC
  behaviour.
- Never bump the Python or OS baseline without confirming every
  deployed Pi can run it. A rollout of an incompatible base is a
  fleet-wide brick.
- Any change to the manifest schema requires a Dashboard contract
  change AND a graceful-fallback path in the parser: ignore
  unknown fields, warn on missing required fields, keep playing
  the last known good manifest if the new one fails to parse.
- Feature flags default OFF for the whole fleet. Individual stands
  are opted in via Dashboard admin, not by editing files on the
  Pi.

---

## Security

- Never store Dashboard credentials in the manifest file or the
  media cache. The auth token lives in a root-owned file
  (`/etc/adnova-player/token`, mode `0600`) installed at
  provisioning time.
- Media files are treated as untrusted content — VLC runs under a
  dedicated non-root user with no shell. There is no code path
  from the manifest to shell execution.
- No SSH-back-in convenience. Remote management is always: Player
  calls Dashboard, never the other way around. If we need a
  remote-diagnostics tunnel it goes through a signed, revocable,
  time-limited token from Dashboard.
- Log files may contain slot IDs and ad IDs, but never full
  media URLs with signed tokens or customer network info beyond
  the local IP.

---

## Watchdog priorities

When something goes wrong, the watchdog protects these in order:

1. **Keep pixels on the screen.** Fall back to the bundled loop
   before showing black. A working "wrong ad" is better than
   working nothing.
2. **Keep the heartbeat going.** An admin who sees "silent" needs
   to distinguish "network down" from "hardware down". Send even
   a minimal heartbeat if the full one fails to serialise.
3. **Ship logs on the next successful heartbeat.** Buffered logs
   flush in bounded batches so a long outage doesn't produce a
   megabyte upload when it comes back.
4. **Self-restart as a last resort.** The systemd unit auto-
   restarts on crash with exponential backoff.

---

## Field debugging

If the user is standing next to a Pi that's misbehaving, the
expected diagnostic flow is:

- `systemctl status adnova-player` → is the service running?
- `journalctl -u adnova-player -n 200` → recent log
- `sqlite3 /var/lib/adnova-player/cache.db "select * from
  current_slot"` → what did we think we were playing?
- `curl -s https://dashboard.adnovatech.online/api/health` →
  network reach from the Pi

Never add UI to the playback screen itself — the display is for
ads, not diagnostics.
